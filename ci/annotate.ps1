# Emit files as workflow annotations (readable through the Checks API even when nothing else can be fetched).
param([string[]]$Files)
foreach ($f in $Files) {
    if (-not (Test-Path $f)) { Write-Host "::warning title=$f::missing"; continue }
    $chunks = New-Object System.Collections.Generic.List[string]
    $cur = New-Object System.Text.StringBuilder
    foreach ($line in (Get-Content $f -Encoding utf8)) {
        $e = ($line -replace '%', '%25' -replace "`r", '' ) + '%0A'
        if ($cur.Length + $e.Length -gt 24000) { $chunks.Add($cur.ToString()); [void]$cur.Clear() }
        [void]$cur.Append($e)
    }
    $chunks.Add($cur.ToString())
    $n = 0
    foreach ($c in $chunks) { $n++; if ($n -le 9) { Write-Host "::notice title=$f $n/$($chunks.Count)::$c" } }
}
