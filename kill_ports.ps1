$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen
foreach ($c in $conns) {
    Stop-Process -Id $c.OwningProcess -Force
}