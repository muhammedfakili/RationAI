"""
Agent module for RationAI - LLM-driven agent using Groq with manual tool parsing.
"""

import os
import json
import logging
import re
from typing import Dict, List, Any, Optional
from typing_extensions import TypedDict
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, END

# Import local modules
from .database import get_db
from .models import Feed
from .rag import query_documents, ingest_documents
from .optimization import (
    optimize_ration,
    optimization_result_to_schema,
    FeedInput,
    NutrientConstraints,
    OptimizationResult,
    calculate_optimal_ration,
    FeedIngredient,
    feeds_from_db
)
from .schemas import (
    OptimizationResponse,
    AgentResponse,
    sanitize_nutrient_constraints,
    validate_ration_feasibility
)

logger = logging.getLogger(__name__)


# Define the agent state
class AgentState(TypedDict):
    messages: List[HumanMessage | AIMessage]
    context: Dict[str, Any]


# Tool 1: Search knowledge base
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the RationAI knowledge base for relevant information about animal nutrition,
    feed ingredients, nutritional requirements, and feed formulation.

    Args:
        query: The search query string

    Returns:
        A string containing relevant information from the knowledge base
    """
    try:
        results = query_documents(query_text=query, n_results=3)
        if results:
            return "\n\n".join(results)
        else:
            return "No relevant information found in the knowledge base."
    except Exception as e:
        return f"Error searching knowledge base: {str(e)}"


# Tool 2: Get feed data from database
@tool
def get_feed_data() -> str:
    """
    Retrieve available feed ingredients and their nutritional composition from the database.

    Returns:
        A string containing feed information
    """
    try:
        # Get a database session
        db_gen = get_db()
        db = next(db_gen)

        try:
            feeds = db.query(Feed).all()
            if not feeds:
                return "No feed data found in database."

            # Format the feed data
            feed_info = ["Available Feed Ingredients:"]
            for feed in feeds:
                feed_info.append(
                    f"- {feed.name}: "
                    f"Price: {feed.price_per_kg}/kg, "
                    f"Dry Matter: {feed.dry_matter}%, "
                    f"Crude Protein: {feed.crude_protein}%, "
                    f"Metabolizable Energy: {feed.metabolic_energy} Mcal/kg DM, "
                    f"Calcium: {feed.calcium} g/kg DM, "
                    f"Phosphorus: {feed.phosphorus} g/kg DM"
                )

            return "\n".join(feed_info)
        finally:
            db.close()
    except Exception as e:
        return f"Error retrieving feed data: {str(e)}"


# Tool 3: Calculate optimal ration (enhanced version)
@tool
def calculate_optimal_ration_enhanced(
    feed_names: str,
    constraints: str,
    animal_weight: float = 600.0,
    force_use_all_feeds: bool = True,
    min_inclusion_pct_dm: float = 3.0
) -> str:
    """
    Calculate the optimal feed ration: meet the nutrient targets first, then minimise cost.

    IMPORTANT: list EVERY feed the user named in `feed_names`. Never drop one to make
    the ration cheaper - the solver decides the amounts, you only pass the candidates.

    Args:
        feed_names: Comma-separated list of feed ingredient names to consider (e.g., "Mısır,Soya Küspesi,Buğday,Yulaf,Mısır Silajı,Yonca Kuru Otu")
        constraints: JSON string containing nutrient constraints (see format below)
        animal_weight: Animal body weight in kg (default 600 kg). Used to estimate daily intake if not provided in constraints.
        force_use_all_feeds: Keep True (default) so every listed feed enters the ration. Only set False if the user explicitly asks for the cheapest subset.
        min_inclusion_pct_dm: Target minimum share per feed (default 3.0% DM). Automatically reduced for minerals.

    Format for constraints (all values on DM basis):
    {
        "crude_protein": {"min": 16.0, "max": 18.0},   // % DM
        "metabolizable_energy": {"min": 2.5, "max": 3.0},  // Mcal/kg DM
        "calcium": {"min": 6.0, "max": 10.0},          // g/kg DM  (= % x 10!)
        "phosphorus": {"min": 3.5, "max": 5.0},        // g/kg DM  (= % x 10!)
        "dry_matter": {"min": 40.0, "max": 60.0},      // % as-fed; OMIT when silage is used
        "daily_intake_kg": 22.0  // optional, auto-calculated from animal_weight if omitted
    }

    Returns:
        A string containing the optimization results
    """
    try:
        # Parse inputs - support both Turkish and English feed names
        feed_name_list = [name.strip() for name in feed_names.split(",") if name.strip()]
        constraints_dict = json.loads(constraints)

        # Auto-calculate daily intake from animal weight if not provided
        # Typical dairy cow DMI = 3.7% BW (kg DM/day). This constraint is DM BASIS.
        if constraints_dict.get("daily_intake_kg") is None:
            dm_intake_kg = animal_weight * 0.037
            constraints_dict["daily_intake_kg"] = dm_intake_kg  # DM basis (kg DM/day)

        # NOT: Kuru madde aralığı için varsayılan ATANMAZ.
        # Rasyonun as-fed KM'si tamamen seçilen yemlere bağlıdır (silajlı rasyon ~%35,
        # tamamen kuru rasyon ~%88). Sabit bir aralık dayatmak, kullanıcı istemediği
        # halde yem seçimini kısıtlar ve modeli gereksiz yere gevşetmeye zorlar.
        # Kullanıcı açıkça belirtirse constraints içinde gelir ve uygulanır.

        # Get feed data from database - use case-insensitive matching
        db_gen = get_db()
        db = next(db_gen)

        try:
            # Build a query that matches feeds case-insensitively
            from sqlalchemy import or_
            from sqlalchemy.orm import joinedload

            # First get all feeds to build a name map for fuzzy matching
            all_feeds = db.query(Feed).all()

            # Create a normalized name map for fuzzy matching
            name_map = {}
            for feed in all_feeds:
                name_map[feed.name.lower()] = feed.name
                # Also add common English translations
                english_map = {
                    'mısır': 'corn',
                    'buğday': 'wheat',
                    'arpa': 'barley',
                    'yulaf': 'oats',
                    'soya küspesi': 'soybean meal',
                    'soya-fullfat': 'full-fat soybean',
                    'mısır gluten yemi': 'corn gluten feed',
                    'mısır kepeği': 'corn bran',
                    'buğday kepeği': 'wheat bran',
                    'yonca kuru otu': 'alfalfa hay',
                    'mısır silajı': 'corn silage',
                    'çayır otu': 'meadow hay',
                    'saman': 'straw',
                }
                turkish_lower = feed.name.lower()
                if turkish_lower in english_map:
                    name_map[english_map[turkish_lower]] = feed.name

            # Find matching feeds
            matched_feeds = []
            for requested_name in feed_name_list:
                req_lower = requested_name.lower()
                if req_lower in name_map:
                    matched_feeds.append(name_map[req_lower])
                else:
                    # Try partial matching
                    for db_name, canonical_name in name_map.items():
                        if req_lower in db_name or db_name in req_lower:
                            matched_feeds.append(canonical_name)
                            break

            # Fallback: if no matches found, use ALL available feeds (robust default)
            if not matched_feeds:
                logger.info("Yem eşleşmesi yok (%s), tüm yemlere düşülüyor", feed_name_list)
                matched_feeds = [f.name for f in all_feeds]

            feeds_db = db.query(Feed).filter(Feed.name.in_(matched_feeds)).all()

            # Convert to FeedIngredient objects using proper DM-basis conversion
            feed_inputs = feeds_from_db(feeds_db)

            if not feed_inputs:
                return f"Hata: Seçilen yemler için besin değeri hesaplanamadı."

            # Convert feed_inputs to dict format for validation
            feeds_for_validation = [f.to_dict() for f in feed_inputs]

            # SANITIZE: Apply validation guardrails to constraints before passing to solver
            constraints_dict = {
                "crude_protein": {"min": constraints_dict.get("crude_protein", {}).get("min"),
                                  "max": constraints_dict.get("crude_protein", {}).get("max")},
                "metabolizable_energy": {"min": constraints_dict.get("metabolizable_energy", {}).get("min"),
                                          "max": constraints_dict.get("metabolizable_energy", {}).get("max")},
                "calcium": {"min": constraints_dict.get("calcium", {}).get("min"),
                            "max": constraints_dict.get("calcium", {}).get("max")},
                "phosphorus": {"min": constraints_dict.get("phosphorus", {}).get("min"),
                               "max": constraints_dict.get("phosphorus", {}).get("max")},
                "dry_matter": {"min": constraints_dict.get("dry_matter", {}).get("min"),
                               "max": constraints_dict.get("dry_matter", {}).get("max")},
                "daily_intake_kg": constraints_dict.get("daily_intake_kg")
            }

            # Calculate expected DM intake from animal_weight for normalization
            expected_dm_intake = animal_weight * 0.037

            # Apply sanitization (clamps to physical bounds for dairy cattle)
            sanitized_constraints = sanitize_nutrient_constraints(
                constraints_dict, expected_daily_intake_kg=expected_dm_intake
            )

            # Validate feasibility against available feeds
            feasibility = validate_ration_feasibility(feeds_for_validation, sanitized_constraints)

            nutrient_constraints = NutrientConstraints(
                crude_protein_min=sanitized_constraints.get("crude_protein", {}).get("min"),
                crude_protein_max=sanitized_constraints.get("crude_protein", {}).get("max"),
                metabolic_energy_min=sanitized_constraints.get("metabolizable_energy", {}).get("min"),
                metabolic_energy_max=sanitized_constraints.get("metabolizable_energy", {}).get("max"),
                calcium_min=sanitized_constraints.get("calcium", {}).get("min"),
                calcium_max=sanitized_constraints.get("calcium", {}).get("max"),
                phosphorus_min=sanitized_constraints.get("phosphorus", {}).get("min"),
                phosphorus_max=sanitized_constraints.get("phosphorus", {}).get("max"),
                dry_matter_min=sanitized_constraints.get("dry_matter", {}).get("min"),
                dry_matter_max=sanitized_constraints.get("dry_matter", {}).get("max"),
                daily_intake_kg=sanitized_constraints.get("daily_intake_kg")
            )

            # Solve: nutrition-first two-phase optimization
            max_asfed_intake = (animal_weight * 0.05) / 0.35
            result: OptimizationResult = optimize_ration(
                feed_inputs,
                nutrient_constraints,
                use_all_selected=force_use_all_feeds,
                min_inclusion_pct_dm=min_inclusion_pct_dm,
                expected_daily_intake_kg=expected_dm_intake,
                max_daily_intake_kg=max_asfed_intake
            )

            # Add feasibility warnings to adjustments if any
            if not feasibility["feasible"]:
                result.adjustments.extend(feasibility["warnings"])

            # Convert to structured schema for the API response
            structured_result = optimization_result_to_schema(result, feed_inputs)

            # Store structured result in context for the API to return
            # Note: We can't directly access state from here, but we'll return a marker
            # and the response will be built from the structured data

            # Format the results for the LLM
            output = [
                "=== RATION OPTIMIZATION RESULT ===",
                f"Status: {result.status}",
                f"Total Daily Cost: {result.total_cost_per_day:.2f} TL/gün",
                f"Ration Cost: {result.total_cost_per_kg:.2f} TL/kg",
                f"Daily Intake: {result.total_daily_intake_kg:.2f} kg/gün (doğal hali)",
                "",
                "YEM ÖNERİLERİ (kg/gün):"
            ]

            for feed_name, amount in sorted(result.feed_amounts.items(), key=lambda x: -x[1]):
                if amount > 0.001:
                    pct = result.feed_proportions.get(feed_name, 0) * 100
                    output.append(f"  - {feed_name}: {amount:.3f} kg/gün ({pct:.1f}%)")

            output.append("")
            output.append("RASYON BESİN DEĞERİ (KM Bazında):")
            output.append(f"  - Ham Protein: {result.nutrient_content.get('crude_protein_percent_dm', 0):.2f}% KM")
            output.append(f"  - Met. Enerji: {result.nutrient_content.get('metabolic_energy_mcal_per_kg_dm', 0):.3f} Mcal/kg KM")
            output.append(f"  - Kalsiyum: {result.nutrient_content.get('calcium_g_per_kg_dm', 0):.2f} g/kg KM")
            output.append(f"  - Fosfor: {result.nutrient_content.get('phosphorus_g_per_kg_dm', 0):.2f} g/kg KM")
            output.append(f"  - Kuru Madde: {result.nutrient_content.get('dry_matter_percent', 0):.1f}% (doğal hali)")
            ca_p = result.nutrient_content.get('ca_p_ratio', 0)
            if ca_p:
                output.append(f"  - Ca:P Oranı: {ca_p:.2f}")

            if result.constraints_status:
                output.append("")
                output.append("KISITLAMA DURUMU:")
                for constraint, status in result.constraints_status.items():
                    icon = "✓" if status == "OK" else "⚠"
                    output.append(f"  {icon} {constraint}: {status}")

            if result.adjustments:
                output.append("")
                output.append("TÜRKÇE AÇIKLAMALAR:")
                for adj in result.adjustments:
                    output.append(f"  - {adj}")

            if result.warnings:
                output.append("")
                output.append("UYARILAR:")
                for w in result.warnings:
                    output.append(f"  - {w}")

            # Return formatted string with structured data marker
            # The tool executor will extract the structured result
            return "\n".join(output) + f"\n\n__STRUCTURED_RESULT__:{json.dumps(structured_result.model_dump())}"

        finally:
            db.close()

    except Exception as e:
        logger.exception("calculate_optimal_ration_enhanced başarısız")
        return f"Hata: Optimizasyon hesaplanamadı - {str(e)}"


# Tool 4: Simple ration calculator (backward compatibility)
@tool
def calculate_simple_ration(
    animal_weight: str,
    target_protein_kg: str = "",
    target_energy_mcal: str = ""
) -> str:
    """
    Calculate a simple ration based on animal weight (enhanced with soft constraints).

    Args:
        animal_weight: Animal weight in kg (as string)
        target_protein_kg: Required crude protein in kg (optional, as string)
        target_energy_mcal: Required metabolizable energy in Mcal (optional, as string)

    Returns:
        A string containing the optimization results with structured data
    """
    try:
        # Convert inputs
        weight = float(animal_weight)
        protein = float(target_protein_kg) if target_protein_kg.strip() else None
        energy = float(target_energy_mcal) if target_energy_mcal.strip() else None

        # Get database session
        db_gen = get_db()
        db = next(db_gen)

        try:
            # Get all feeds from database and convert properly
            feeds_db = db.query(Feed).all()
            if not feeds_db:
                return "Hata: Veritabanında yem verisi bulunamadı."

            # Convert using proper DM-basis conversion
            feed_inputs = feeds_from_db(feeds_db)

            # Estimate daily intake (~3.7% BW for dairy cows) - DM BASIS
            expected_dm_intake = weight * 0.037
            # Max as-fed intake safety: assume min 35% DM in ration, so max as-fed = DM_intake / 0.35
            max_asfed_intake = (weight * 0.05) / 0.35  # 5% BW DM intake / 0.35 = ~14% BW as-fed

            # Build constraints - use simple defaults if not provided
            if protein is None:
                # Rough estimate: ~1.8 kg CP/day for 600kg cow
                protein = weight * 0.003
            if energy is None:
                # Rough estimate: ~35 Mcal ME/day for 600kg cow
                energy = weight * 0.035 + 5.0

            # The tool takes DAILY totals (kg CP/day, Mcal ME/day) but the solver works on a
            # DM-concentration basis, so divide by the expected daily DM intake.
            protein_pct_dm = (protein / expected_dm_intake) * 100.0
            energy_mcal_per_kg_dm = energy / expected_dm_intake

            # Convert feed_inputs to dict for validation
            feeds_for_validation = [f.to_dict() for f in feed_inputs]

            # SANITIZE: Apply validation guardrails
            constraints_dict = {
                "crude_protein": {"min": protein_pct_dm, "max": None},
                "metabolizable_energy": {"min": energy_mcal_per_kg_dm, "max": None},
                "daily_intake_kg": expected_dm_intake
            }

            from .schemas import sanitize_nutrient_constraints, validate_ration_feasibility
            sanitized_constraints = sanitize_nutrient_constraints(constraints_dict, expected_daily_intake_kg=expected_dm_intake)

            # Validate feasibility
            feasibility = validate_ration_feasibility(feeds_for_validation, sanitized_constraints)

            constraints = NutrientConstraints(
                crude_protein_min=sanitized_constraints.get("crude_protein", {}).get("min"),
                crude_protein_max=sanitized_constraints.get("crude_protein", {}).get("max"),
                metabolic_energy_min=sanitized_constraints.get("metabolizable_energy", {}).get("min"),
                metabolic_energy_max=sanitized_constraints.get("metabolizable_energy", {}).get("max"),
                dry_matter_min=sanitized_constraints.get("dry_matter", {}).get("min"),
                dry_matter_max=sanitized_constraints.get("dry_matter", {}).get("max"),
                daily_intake_kg=sanitized_constraints.get("daily_intake_kg")
            )

            # Solve: nutrition-first two-phase optimization.
            # This tool considers the WHOLE feed database, so feeds are not forced in -
            # the solver picks the cheapest combination that meets the targets.
            result: OptimizationResult = optimize_ration(
                feed_inputs,
                constraints,
                use_all_selected=False,
                expected_daily_intake_kg=expected_dm_intake,
                max_daily_intake_kg=max_asfed_intake
            )

            # Add feasibility warnings to adjustments if any
            if not feasibility["feasible"]:
                result.adjustments.extend(feasibility["warnings"])

            # Convert to structured schema
            structured_result = optimization_result_to_schema(result, feed_inputs)

            # Format text output for LLM
            output = [
                "=== BASİT RASYON HESAPLAMA SONUCU ===",
                f"Hayvan Ağırlığı: {weight} kg",
                f"Durum: {result.status}",
                f"Toplam Günlük Maliyet: {result.total_cost_per_day:.2f} TL/gün",
                f"Rasyon Maliyeti: {result.total_cost_per_kg:.2f} TL/kg",
                f"Günlük Tüketim: {result.total_daily_intake_kg:.2f} kg/gün (doğal hali)",
                "",
                "YEM ÖNERİLERİ (kg/gün):"
            ]

            for feed_name, amount in sorted(result.feed_amounts.items(), key=lambda x: -x[1]):
                if amount > 0.001:
                    pct = result.feed_proportions.get(feed_name, 0) * 100
                    output.append(f"  - {feed_name}: {amount:.3f} kg/gün ({pct:.1f}%)")

            output.append("")
            output.append("RASYON BESİN DEĞERİ (KM Bazında):")
            output.append(f"  - Ham Protein: {result.nutrient_content.get('crude_protein_percent_dm', 0):.2f}% KM")
            output.append(f"  - Met. Enerji: {result.nutrient_content.get('metabolic_energy_mcal_per_kg_dm', 0):.3f} Mcal/kg KM")
            output.append(f"  - Kalsiyum: {result.nutrient_content.get('calcium_g_per_kg_dm', 0):.2f} g/kg KM")
            output.append(f"  - Fosfor: {result.nutrient_content.get('phosphorus_g_per_kg_dm', 0):.2f} g/kg KM")
            output.append(f"  - Kuru Madde: {result.nutrient_content.get('dry_matter_percent', 0):.1f}% (doğal hali)")

            if result.adjustments:
                output.append("")
                output.append("TÜRKÇE AÇIKLAMALAR:")
                for adj in result.adjustments:
                    output.append(f"  - {adj}")

            # Return with structured data marker
            return "\n".join(output) + f"\n\n__STRUCTURED_RESULT__:{json.dumps(structured_result.model_dump())}"

        finally:
            db.close()

    except Exception as e:
        logger.exception("calculate_simple_ration başarısız")
        return f"Hata: Basit rasyon hesaplanamadı - {str(e)}"


# ============================================================
# LLM (Groq) — lazily constructed on first use.
#
# ChatGroq raises at construction time when GROQ_API_KEY is unset. Building it at
# import time therefore took the WHOLE backend down with it — including the
# deterministic /optimize path, which never touches an LLM. Everything except
# /api/v1/agent/query must keep working without a key.
# ============================================================

GROQ_MODEL = "llama-3.3-70b-versatile"

_llm: Optional[ChatGroq] = None


def llm_available() -> bool:
    """True when a Groq API key is configured (no network call, no client built)."""
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def _get_llm() -> ChatGroq:
    """Return the shared ChatGroq client, building it on first use.

    Raises:
        RuntimeError: when GROQ_API_KEY is unset — callers turn this into HTTP 503.
    """
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


# Agent node: uses LLM to decide which tool to call and extract parameters
def agent_node(state: AgentState):
    """
    Main agent node that uses the LLM to reason about the user query,
    decide which tool to use, and extract its arguments.
    If no tool is needed, the LLM provides a direct answer.
    """
    messages = state["messages"]
    # The last message is the user's query (HumanMessage)
    user_query = messages[-1].content if isinstance(messages[-1], HumanMessage) else ""

    # Build a system message that instructs the LLM how to use tools
    system_prompt = """You are an AI assistant that helps with animal nutrition questions. You have access to the following tools:

1. search_knowledge_base:
   - Description: Search the RationAI knowledge base for relevant information about animal nutrition, feed ingredients, nutritional requirements, and feed formulation.
   - Parameters:
     - query (string): The search query string
   - Returns: Relevant knowledge base snippets

2. get_feed_data:
   - Description: Retrieve available feed ingredients and their nutritional composition from the database.
   - Parameters: None
   - Returns: List of feed ingredients with price, dry matter, crude protein, metabolizable energy, calcium, phosphorus

3. calculate_optimal_ration_enhanced:
   - Description: Nutrition-first ration optimizer. It meets the nutrient targets FIRST and
     minimises cost only among rations that satisfy them.
   - Parameters:
     - feed_names (string): Comma-separated list of feed ingredient names to consider (e.g., "Mısır,Soya Küspesi,Buğday,Yulaf,Mısır Silajı,Yonca Kuru Otu"). Use Turkish feed names from get_feed_data.
       ⚠️ List EVERY feed the user mentioned. Never leave one out to make the ration cheaper -
       the solver decides the amounts; your job is only to pass the candidate list.
     - constraints (string): JSON string containing nutrient constraints with min/max values (all on DM basis):
        * crude_protein (%) - typical dairy: 16-18%
        * metabolizable_energy (Mcal/kg) - typical dairy: 2.5-2.8
        * calcium (g/kg DM) - typical dairy: 6-10. This is TEN TIMES the percentage (0.6% = 6 g/kg).
        * phosphorus (g/kg DM) - typical dairy: 3.5-5. Also ten times the percentage.
        * dry_matter (%) - OMIT THIS unless the user explicitly asks. It depends entirely on
          which feeds are chosen (silage ration ~35%, all-dry ration ~88%) and a wrong
          value makes the targets unreachable.
        * daily_intake_kg (optional, kg DM/day) - auto-calculated from animal_weight if omitted
     - animal_weight (float, optional): Animal body weight in kg (default 600). Used to auto-estimate daily intake if not in constraints.
     - force_use_all_feeds (bool, optional): defaults to true - keep it that way so every
       listed feed enters the ration.
   - Returns: Optimal ration formulation with feed amounts, nutrient content, total cost, and constraint status

4. calculate_simple_ration:
   - Description: Calculate a simple ration based on animal weight (backward compatible).
   - Parameters:
     - animal_weight (string): Animal weight in kg
     - target_protein_kg (string, optional): Required crude protein in kg
     - target_energy_mcal (string, optional): Required metabolizable energy in Mcal
   - Returns: Simple ration formulation

Based on the user's question, decide which tool to use and provide the arguments. If you can answer directly without tools, respond with a plain answer.

IMPORTANT: Feed names in our database are in Turkish. Examples:
- Mısır (Corn)
- Buğday (Wheat)
- Arpa (Barley)
- Yulaf (Oats)
- Soya Küspesi (Soybean Meal)
- Soya-Fullfat (Full-fat Soybean)
- Mısır Gluten Yemi (Corn Gluten Feed)
- Mısır Kepeği (Corn Bran)
- Buğday Kekeği (Wheat Bran)
- DDGS (DDGS)
- Yonca Kuru Otu (Alfalfa Hay)
- Mısır Silajı (Corn Silage)
- Çayır Otu (Meadow Hay)
- Saman (Straw)

For TMR rations with silage: use dry_matter range 40-60% (not 85-90%).
For daily intake: if you provide animal_weight, the tool auto-calculates it (~22 kg DM/day for 600 kg cow = ~44 kg as-fed).

You MUST respond with a valid JSON object exactly matching one of these formats:

If using a tool:
{
  "tool": "tool_name_exact",
  "args": {
    "param1": "value1",
    "param2": "value2"
  }
}

If not using a tool (direct answer):
{
  "answer": "your answer here"
}

Do not include any extra text outside the JSON. Be precise with parameter names and types.
"""

    # Prepare messages for LLM: system + conversation history (we only need the latest user query for simplicity)
    # We'll include the system prompt and the user query.
    lc_messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_query)]

    # Invoke the LLM
    ai_msg: AIMessage = _get_llm().invoke(lc_messages)

    # Try to parse the response as JSON
    response_text = ai_msg.content.strip()
    tool_name = None
    tool_args = {}
    direct_answer = None

    # Attempt to extract JSON from the response (in case the LLM adds extra text)
    json_match = re.search(r'\{[\s\S]*\}', response_text)
    if json_match:
        json_str = json_match.group(0)
        try:
            data = json.loads(json_str)
            if "tool" in data and "args" in data:
                tool_name = data["tool"]
                tool_args = data.get("args", {})
            elif "answer" in data:
                direct_answer = data["answer"]
            else:
                # If we can't determine, treat as direct answer
                direct_answer = response_text
        except json.JSONDecodeError:
            # If JSON parsing fails, treat the whole response as direct answer
            direct_answer = response_text
    else:
        # No JSON found, treat as direct answer
        direct_answer = response_text

    logger.debug("LLM ham yanıtı: %s", response_text)
    logger.debug("tool_name=%s, tool_args=%s", tool_name, tool_args)

    new_context = dict(state.get("context", {}))

    if tool_name is not None:
        # Keep the raw message: the tool executor appends the readable result after it.
        new_messages = messages + [ai_msg]
        new_context["tool_name"] = tool_name
        new_context["tool_args"] = tool_args
        new_context["tool_called"] = True
    else:
        # No tool needed. Append the EXTRACTED answer, not the raw message: the LLM replies
        # with a JSON envelope ({"answer": "..."}) and `run_agent` returns the last AIMessage.
        # Appending `ai_msg` here made users see that raw JSON as the agent's answer.
        new_messages = messages + [AIMessage(content=direct_answer)]
        new_context["tool_called"] = False
        new_context["direct_answer"] = direct_answer

    return {
        "messages": new_messages,
        "context": new_context
    }


# Tool executor node: runs the selected tool
def tool_executor_node(state: AgentState):
    """
    Executes the tool selected by the LLM.
    """
    context = state.get("context", {})
    tool_name = context.get("tool_name")
    tool_args = context.get("tool_args", {})

    # Map tool names to the actual tool functions
    tools_map = {
        "search_knowledge_base": search_knowledge_base,
        "get_feed_data": get_feed_data,
        "calculate_optimal_ration_enhanced": calculate_optimal_ration_enhanced,
        "calculate_simple_ration": calculate_simple_ration
    }

    if tool_name not in tools_map:
        result = f"Error: Unknown tool '{tool_name}'"
        structured_result = None
    else:
        try:
            # Invoke the tool with the provided arguments
            tool_func = tools_map[tool_name]
            result = tool_func.invoke(tool_args)

            # Extract structured result if present (for optimization tools)
            if tool_name in ("calculate_optimal_ration_enhanced", "calculate_simple_ration") and isinstance(result, str) and "__STRUCTURED_RESULT__:" in result:
                try:
                    parts = result.split("__STRUCTURED_RESULT__:")
                    clean_result = parts[0]
                    structured_json = parts[1]
                    # Validate through the schema, then hand back a plain dict:
                    # AgentResponse.optimization_result is Dict[str, Any], not a model.
                    structured_result = OptimizationResponse(
                        **json.loads(structured_json)
                    ).model_dump()
                    result = clean_result  # Keep clean text for LLM
                except Exception as e:
                    logger.warning("Yapısal optimizasyon sonucu ayrıştırılamadı: %s", e)
                    structured_result = None
            else:
                structured_result = None

        except Exception as e:
            result = f"Error executing tool {tool_name}: {str(e)}"
            structured_result = None

    # Create an AIMessage with the result (as if the agent produced it)
    ai_message = AIMessage(content=result)

    # Update state: add the tool result message and clear tool-specific context
    new_messages = state["messages"] + [ai_message]
    new_context = dict(state["context"])
    # Remove tool-specific fields to avoid reuse
    new_context.pop("tool_name", None)
    new_context.pop("tool_args", None)
    new_context.pop("tool_called", None)
    new_context.pop("direct_answer", None)
    # Store structured optimization result for API response
    if structured_result:
        new_context["optimization_result"] = structured_result

    return {
        "messages": new_messages,
        "context": new_context
    }


# Build the graph
def create_agent_graph():
    """
    Create the LangGraph state graph for the agent.
    """
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("agent", agent_node)
    workflow.add_node("tool_executor", tool_executor_node)

    # Set entry point
    workflow.set_entry_point("agent")

    # Add conditional edge: after agent, go to tool_executor if a tool was called, else end
    def should_continue(state: AgentState) -> str:
        if state.get("context", {}).get("tool_called"):
            return "tool_executor"
        else:
            return END

    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tool_executor": "tool_executor",
            END: END
        }
    )

    # After tool execution, always end
    workflow.add_edge("tool_executor", END)

    # Compile the graph
    return workflow.compile()


# Create the agent instance
agent_graph = create_agent_graph()


def run_agent(query: str) -> AgentResponse:
    """
    Run the agent on a user query and return a structured response.

    Args:
        query: The user's question or request

    Returns:
        AgentResponse with both text response and optional structured optimization data

    Raises:
        Exception: whatever the graph raised, after logging it — the caller decides
        how to surface it (the API endpoint turns it into HTTP 500).
    """
    # Initialize state
    initial_state = {
        "messages": [HumanMessage(content=query)],
        "context": {}
    }

    try:
        # Run the agent
        final_state = agent_graph.invoke(initial_state)

        # Get the last AI message as the text response
        messages = final_state["messages"]
        text_response = "I'm sorry, I couldn't process your request."
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                text_response = msg.content
                break

        # Extract structured optimization result from context
        optimization_result = final_state.get("context", {}).get("optimization_result")

        return AgentResponse(
            success=True,
            answer=text_response,
            response=text_response,
            optimization_result=optimization_result
        )
    except Exception:
        # Deliberately not turned into a success=False AgentResponse: a crash in the
        # graph is a server error and the endpoint must be able to answer 500 with it.
        logger.exception("run_agent başarısız (query=%r)", query)
        raise


# Optional: Function to ingest documents (can be called separately)
def initialize_knowledge_base():
    """
    Initialize the knowledge base by ingesting documents from the data directory.
    This can be called during application startup.
    """
    try:
        ingest_documents()
        return "Knowledge base initialized successfully."
    except Exception as e:
        return f"Error initializing knowledge base: {str(e)}"