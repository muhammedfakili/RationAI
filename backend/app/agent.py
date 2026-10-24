"""
Agent module for RationAI - LLM-driven agent using Groq with manual tool parsing.

The LLM does not use native function calling: `agent_node` asks for a JSON tool call
and `tool_executor_node` parses and runs it. Graph: agent -> tool_executor -> END.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph

from .database import SessionLocal
from .models import Feed
from .optimization import (
    RULES_BY_GROUP,
    NutrientConstraints,
    feeds_from_db,
    format_result_for_agent,
    optimization_result_to_schema,
    optimize_ration,
)
from .rag import query_documents
from .requirements import AnimalProfile, compute_requirements, requirements_to_constraints
from .schemas import AgentResponse

logger = logging.getLogger(__name__)

# Minerals the UI pre-selects; added when the LLM names no mineral, otherwise Ca/P
# can only come from forages and the ration lands in relaxed mode.
DEFAULT_MINERALS = ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz")

# English names the LLM sometimes uses -> database name.
_ENGLISH_FEED_NAMES = {
    "corn": "Mısır",
    "wheat": "Buğday",
    "barley": "Arpa",
    "oats": "Yulaf",
    "soybean meal": "Soya Küspesi",
    "full-fat soybean": "Soya-Fullfat",
    "corn gluten feed": "Mısır Gluten Yemi",
    "corn bran": "Mısır Kepeği",
    "wheat bran": "Buğday Kepeği",
    "alfalfa hay": "Yonca Kuru Otu",
    "corn silage": "Mısır Silajı",
    "meadow hay": "Çayır Otu",
    "straw": "Buğday Samanı",
}

# Physically sensible ranges for LLM-supplied constraints (DM basis).
NUTRIENT_BOUNDS = {
    "crude_protein": (6.0, 30.0),           # % DM
    "metabolizable_energy": (1.5, 3.5),     # Mcal/kg DM
    "net_energy_lactation": (0.9, 2.0),     # Mcal/kg DM
    "calcium": (1.0, 15.0),                 # g/kg DM
    "phosphorus": (1.0, 10.0),              # g/kg DM
    "dry_matter": (20.0, 100.0),            # % as-fed
}
# Constraint key the LLM uses -> NutrientConstraints field prefix.
_CONSTRAINT_FIELDS = {
    "crude_protein": "crude_protein",
    "metabolizable_energy": "metabolic_energy",
    "net_energy_lactation": "net_energy_lactation",
    "calcium": "calcium",
    "phosphorus": "phosphorus",
    "dry_matter": "dry_matter",
}

ToolResult = Tuple[str, Optional[Dict[str, Any]]]


class AgentState(TypedDict):
    messages: List[HumanMessage | AIMessage]
    context: Dict[str, Any]


def _to_float(value: Any) -> Optional[float]:
    """Coerce to float; None when missing, unparsable or NaN."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def parse_constraints(raw: Any, expected_dm_intake_kg: Optional[float] = None) -> NutrientConstraints:
    """Turn the LLM's nested constraint JSON into clamped NutrientConstraints.

    Accepts ``{"crude_protein": {"min": 16, "max": 18}, ..., "daily_intake_kg": 22}``
    as a dict or JSON string. Unparsable values are dropped, bounds are clamped to
    `NUTRIENT_BOUNDS`, and inverted min/max are swapped instead of discarded.
    Without an intake in the JSON, `expected_dm_intake_kg` is used (None stays None).
    """
    data = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    values: Dict[str, Optional[float]] = {}

    for key, (lo, hi) in NUTRIENT_BOUNDS.items():
        bounds = data.get(key)
        if isinstance(bounds, dict):
            low, high = _to_float(bounds.get("min")), _to_float(bounds.get("max"))
        else:
            low, high = _to_float(bounds), None  # a bare scalar is read as a minimum
        low = None if low is None else max(lo, min(hi, low))
        high = None if high is None else max(lo, min(hi, high))
        if low is not None and high is not None and low > high:
            low, high = high, low
        field = _CONSTRAINT_FIELDS[key]
        values[f"{field}_min"], values[f"{field}_max"] = low, high

    intake = _to_float(data.get("daily_intake_kg"))
    if intake is None:
        intake = expected_dm_intake_kg
    values["daily_intake_kg"] = None if intake is None else max(0.5, min(60.0, intake))
    return NutrientConstraints(**values)


def _match_feed_names(requested: List[str], all_names: List[str]) -> List[str]:
    """Map LLM feed names onto database names: exact, English alias, then substring."""
    by_lower = {name.lower(): name for name in all_names}
    by_lower.update({en: tr for en, tr in _ENGLISH_FEED_NAMES.items() if tr in all_names})
    matched: List[str] = []
    for name in requested:
        key = name.lower()
        hit = by_lower.get(key) or next(
            (canonical for lower, canonical in by_lower.items() if key in lower or lower in key), None
        )
        if hit and hit not in matched:
            matched.append(hit)
    return matched


# ============================================================
# Tools — each returns (text for the user, structured optimization result or None)
# ============================================================

def search_knowledge_base(query: str) -> ToolResult:
    results = query_documents(query_text=query, n_results=3)
    return ("\n\n".join(results) if results else "Bilgi tabanında ilgili bilgi bulunamadı."), None


def get_feed_data() -> ToolResult:
    with SessionLocal() as db:
        feeds = db.query(Feed).order_by(Feed.feed_type, Feed.name).all()
    if not feeds:
        return "Veritabanında yem verisi yok.", None
    lines = ["Mevcut yemler (besin değerleri KM bazında):"] + [
        f"- {f.name} [{f.feed_type}]: {f.price_per_kg} TL/kg, KM %{f.dry_matter}, "
        f"HP %{f.crude_protein}, NEL {f.net_energy_lactation} Mcal/kg, "
        f"ME {f.metabolic_energy} Mcal/kg, NDF %{f.ndf}, "
        f"Ca {f.calcium} g/kg, P {f.phosphorus} g/kg"
        for f in feeds
    ]
    return "\n".join(lines), None


def calculate_ration(
    feed_names: str = "",
    constraints: Any = None,
    animal_weight: float = 650.0,
    milk_yield: float = 30.0,
    group: str = "lactating",
    days_in_milk: int = 120,
    fat_pct: float = 3.8,
    days_pregnant: int = 0,
    first_lactation: bool = False,
    force_use_all_feeds: bool = True,
    min_inclusion_pct_dm: float = 3.0,
) -> ToolResult:
    """Run the same two-phase optimizer the /optimize endpoint uses.

    Targets come from the NASEM 2021 requirements of the described animal; `constraints`
    only overrides the keys the user stated explicitly. With no (matching) feed names,
    every feed is a candidate and none is forced in.
    """
    group = group if group in RULES_BY_GROUP else "lactating"
    profile = AnimalProfile(
        group=group,
        body_weight_kg=float(animal_weight),
        milk_kg=float(milk_yield or 0.0),
        fat_pct=float(fat_pct),
        days_in_milk=max(1, int(days_in_milk)),
        multiparous=not bool(first_lactation),
        days_pregnant=max(0, int(days_pregnant)),
    )
    req = compute_requirements(profile)
    explicit = parse_constraints(constraints)
    target = requirements_to_constraints(req)
    for name, value in vars(explicit).items():
        if value is not None:
            setattr(target, name, value)
    requested = [n.strip() for n in str(feed_names).split(",") if n.strip()]

    with SessionLocal() as db:
        all_feeds = db.query(Feed).all()
        names = _match_feed_names(requested, [f.name for f in all_feeds])
        if names:
            selected = [f for f in all_feeds if f.name in names]
            if not any(f.feed_type in ("mineral", "salt") for f in selected):
                selected += [f for f in all_feeds if f.name in DEFAULT_MINERALS]
        else:
            logger.info("Yem eşleşmesi yok (%s), tüm yemler aday", requested)
            selected, force_use_all_feeds = all_feeds, False
        feed_inputs = feeds_from_db(selected)

    rules = RULES_BY_GROUP[group]
    result = optimize_ration(
        feed_inputs,
        target,
        use_all_selected=bool(force_use_all_feeds),
        min_inclusion_pct_dm=float(min_inclusion_pct_dm),
        expected_daily_intake_kg=target.daily_intake_kg,
        max_daily_intake_kg=(profile.body_weight_kg * 0.05) / 0.35,
        body_weight_kg=profile.body_weight_kg,
        rules=rules,
    )
    result.adjustments = [
        f"🐄 {rules.label}, {profile.body_weight_kg:.0f} kg"
        + (f", {profile.milk_kg:.0f} kg süt, laktasyonun {profile.days_in_milk}. günü"
           if group == "lactating" else "")
        + f": KMT {req.dm_intake_kg} kg, NEL {req.nel_min}-{req.nel_max} Mcal/kg KM, "
          f"HP %{target.crude_protein_min}-{target.crude_protein_max} (NASEM 2021)."
    ] + [f"ℹ️ {note}" for note in req.notes] + result.adjustments
    structured = optimization_result_to_schema(result, feed_inputs).model_dump()
    return format_result_for_agent(result), structured


TOOLS = {
    "search_knowledge_base": search_knowledge_base,
    "get_feed_data": get_feed_data,
    "calculate_ration": calculate_ration,
}


# ============================================================
# LLM (Groq) — lazily constructed on first use.
#
# ChatGroq raises at construction time when GROQ_API_KEY is unset. Building it at
# import time therefore took the WHOLE backend down with it — including the
# deterministic /optimize path, which never touches an LLM. Everything except
# /api/v1/agent/query must keep working without a key.
# ============================================================

# Groq retires models without much notice (llama-3.3-70b-versatile started answering
# 404 model_not_found), so the model is overridable without a code change.
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"

_llm: Optional[ChatGroq] = None


def llm_available() -> bool:
    """True when a Groq API key is configured (no network call, no client built)."""
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def _get_llm() -> ChatGroq:
    """Return the shared ChatGroq client, building it on first use."""
    global _llm
    if _llm is None:
        if not llm_available():
            raise RuntimeError(
                "GROQ_API_KEY tanımlı değil. Sohbet uç noktası devre dışı; "
                "rasyon hesaplama (/api/v1/optimize) anahtarsız da çalışır."
            )
        # api_key is read from the GROQ_API_KEY environment variable automatically.
        _llm = ChatGroq(temperature=0, model_name=GROQ_MODEL)
    return _llm


SYSTEM_PROMPT = """You are RationAI, a dairy-cow nutrition assistant. Answer in the user's language.
You can call one of these tools:

1. search_knowledge_base
   - args: query (string)
   - Searches the nutrition reference documents.

2. get_feed_data
   - args: none
   - Lists every feed with price and nutrient values.

3. calculate_ration
   - Nutrition-first optimizer: it meets the nutrient targets first and minimises cost
     only among rations that satisfy them.
   - args:
     - feed_names (string): comma-separated feed names, exactly as in the list below.
       List EVERY feed the user mentioned — never leave one out to make it cheaper; the
       solver decides the amounts. Leave empty to let it choose from all feeds.
       Minerals are added automatically when none is listed.
     - Describe the ANIMAL; the tool computes the nutrient targets itself (NASEM 2021).
       Do NOT invent nutrient targets.
       - animal_weight (number, kg, default 650)
       - milk_yield (number, kg milk/day, default 30; a "litre" is ~1 kg)
       - group: "lactating" (default), "dry_faroff" (dry, >3 weeks before calving)
         or "dry_closeup" (last ~3 weeks before calving)
       - days_in_milk (int, default 120), fat_pct (default 3.8),
         days_pregnant (int, 0 = not pregnant), first_lactation (bool, default false)
     - constraints (JSON object) — ONLY targets the user stated explicitly; they override
       the computed ones. DM basis, every key optional:
         "crude_protein": {{"min": 16, "max": 18}}          % DM
         "net_energy_lactation": {{"min": 1.6, "max": 1.8}} Mcal NEL/kg DM
         "calcium": {{"min": 6, "max": 10}}                  g/kg DM = % x 10
         "phosphorus": {{"min": 3.5, "max": 5}}              g/kg DM = % x 10
         "daily_intake_kg": 22                             kg DM/day
       Do NOT send "dry_matter" unless the user explicitly asks: a silage ration sits
       near 35 % DM and a wrong range makes every target unreachable.
     - force_use_all_feeds (bool, default true) — false only if the user wants the
       cheapest subset.

Feeds in the database (Turkish names, grouped by type):
{feed_list}

Respond with ONLY a JSON object, no other text:
- To call a tool: {{"tool": "<tool name>", "args": {{...}}}}
- To answer directly: {{"answer": "<your answer>"}}
"""


def _feed_list_for_prompt() -> str:
    with SessionLocal() as db:
        rows = db.query(Feed.feed_type, Feed.name).order_by(Feed.feed_type, Feed.name).all()
    groups: Dict[str, List[str]] = {}
    for feed_type, name in rows:
        groups.setdefault(feed_type or "unknown", []).append(name)
    return "\n".join(f"- {ftype}: {', '.join(names)}" for ftype, names in groups.items())


def _parse_llm_reply(text: str) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Return (tool_name, tool_args, direct_answer) from the LLM's JSON reply."""
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if "tool" in data:
                return data["tool"], data.get("args") or {}, None
            if "answer" in data:
                return None, {}, str(data["answer"])
    return None, {}, text


def agent_node(state: AgentState):
    """Ask the LLM to either pick a tool (with args) or answer directly."""
    user_query = state["messages"][-1].content
    prompt = SYSTEM_PROMPT.format(feed_list=_feed_list_for_prompt())
    ai_msg: AIMessage = _get_llm().invoke([SystemMessage(content=prompt), HumanMessage(content=user_query)])

    response_text = ai_msg.content.strip()
    from_reasoning = not response_text
    if from_reasoning:
        # gpt-oss sometimes ends its turn inside the reasoning channel, leaving `content`
        # empty with the JSON reply at the end of `reasoning_content`. Use that JSON — but
        # never show raw reasoning to the user.
        reasoning = ai_msg.additional_kwargs.get("reasoning_content", "")
        start = max(reasoning.rfind('{"tool"'), reasoning.rfind('{"answer"'))
        response_text = reasoning[start:] if start >= 0 else ""
    tool_name, tool_args, direct_answer = _parse_llm_reply(response_text)
    unparsed = direct_answer == response_text  # no JSON envelope was found
    if tool_name is None and (not direct_answer.strip() or (from_reasoning and unparsed)):
        direct_answer = "Üzgünüm, bu soruya şu an yanıt üretemedim. Lütfen tekrar deneyin."
    logger.debug("LLM ham yanıtı: %s", response_text)
    logger.debug("tool_name=%s, tool_args=%s", tool_name, tool_args)

    context = dict(state.get("context", {}))
    if tool_name is not None:
        # Keep the raw message: the tool executor appends the readable result after it.
        context.update(tool_name=tool_name, tool_args=tool_args)
        return {"messages": state["messages"] + [ai_msg], "context": context}

    # Append the EXTRACTED answer, not the raw message: the LLM replies with a JSON
    # envelope ({"answer": "..."}) and `run_agent` returns the last AIMessage, so
    # appending `ai_msg` here made users see that raw JSON as the agent's answer.
    return {"messages": state["messages"] + [AIMessage(content=direct_answer)], "context": context}


def tool_executor_node(state: AgentState):
    """Run the tool the LLM picked and append its readable result."""
    context = dict(state["context"])
    tool_name = context.pop("tool_name")
    tool_args = context.pop("tool_args", {})

    tool = TOOLS.get(tool_name)
    if tool is None:
        text, structured = f"Bilinmeyen araç: '{tool_name}'", None
    else:
        try:
            text, structured = tool(**tool_args)
        except Exception as exc:
            logger.exception("Araç %s başarısız (args=%r)", tool_name, tool_args)
            text, structured = f"Araç çalıştırılamadı ({tool_name}): {exc}", None

    if structured:
        context["optimization_result"] = structured
    return {"messages": state["messages"] + [AIMessage(content=text)], "context": context}


def create_agent_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tool_executor", tool_executor_node)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges(
        "agent",
        lambda state: "tool_executor" if "tool_name" in state.get("context", {}) else END,
        {"tool_executor": "tool_executor", END: END},
    )
    workflow.add_edge("tool_executor", END)
    return workflow.compile()


agent_graph = create_agent_graph()


def run_agent(query: str) -> AgentResponse:
    """Run the agent on a user query.

    Raises whatever the graph raised, after logging it — the endpoint turns it into
    HTTP 500 instead of folding a crash into a 200 response.
    """
    try:
        final_state = agent_graph.invoke({"messages": [HumanMessage(content=query)], "context": {}})
    except Exception:
        logger.exception("run_agent başarısız (query=%r)", query)
        raise

    text = next(
        (msg.content for msg in reversed(final_state["messages"]) if isinstance(msg, AIMessage)),
        "Üzgünüm, isteğinizi işleyemedim.",
    )
    return AgentResponse(
        success=True,
        answer=text,
        response=text,
        optimization_result=final_state.get("context", {}).get("optimization_result"),
    )
