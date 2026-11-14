<#
.SYNOPSIS
  Set up autocad-lt-mcp on this Windows machine in one run.

.DESCRIPTION
  Run from the clone, with AutoCAD LT closed:

    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -WhatIf   # show, change nothing
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

  It uses the folder this script sits in as the install, so run it from the clone you
  mean to keep. Each step checks before it writes, so running it twice changes nothing
  the second time.

    1. Checks uv, git and AutoCAD LT 2024+; stops with what to install if one is missing.
    2. uv sync.
    3. Startup Suite: makes mcp_dispatch.lsp from this clone load in every drawing, and
       drops mcp_*.lsp entries that point at another folder (an old or renamed clone).
       Other entries are kept. If SECURELOAD is on, adds lisp-code to TRUSTEDPATHS.
       Registry keys are exported to a .reg backup first. Refuses while AutoCAD runs,
       because AutoCAD writes its profile back on exit and would undo the change.
    4. Registers the server with Claude Code, Antigravity and Codex, whichever are
       installed, and removes the pre-rename `autocad-mcp` entry. Copies the skills
       into Antigravity's and Codex's skills folders, removing pre-rename copies.
    5. Builds the skill zips and prints the claude.ai upload step.

  Backups go to %LOCALAPPDATA%\autocad-lt-mcp\backups\<timestamp>\. Double-click a
  .reg file to restore it.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$Server = 'autocad-lt-mcp'
$OldServer = 'autocad-mcp'
$SkillNames = 'autocad-lt-mcp-workflow', 'autocad-lt-save-verification'
$OldSkillNames = 'autocad-mcp-workflow', 'autocad-save-verification'

# ---------- pure helpers (tests/test_install_script.py dot-sources these) ----------

# Expand: APPLOAD stores entries as %USERPROFILE%\... (REG_EXPAND_SZ), and those are not stale.
function Normalize-Dir([string]$p) { [Environment]::ExpandEnvironmentVariables($p).Trim().TrimEnd('\').ToLowerInvariant() }

function Merge-StartupList([string[]]$Entries, [string]$LispDir) {
    # Drop our files when they live in another folder; keep everything else in order.
    # String splits, not Split-Path: CI runs this on Linux, where Split-Path ignores '\'.
    $want = Normalize-Dir $LispDir
    $out = @($Entries | Where-Object {
        $_ -and -not (
            ($_ -replace '^.*\\', '') -match '^mcp_(dispatch|probes|select)\.lsp$' -and
            (Normalize-Dir ($_ -replace '\\[^\\]*$', '')) -ne $want)
    })
    $dispatch = $LispDir.TrimEnd('\') + '\mcp_dispatch.lsp'
    if (-not ($out | Where-Object { (Normalize-Dir $_) -eq (Normalize-Dir $dispatch) })) { $out += $dispatch }
    , $out
}

function Add-TrustedPath([string]$Current, [string]$Dir, [string]$SecureLoad) {
    # SECURELOAD 0 loads from anywhere, so trusting a folder would only widen the setting.
    if ($SecureLoad -eq '0') { return $Current }
    $parts = @(($Current -split ';') | Where-Object { $_.Trim() })
    if ($parts | Where-Object { (Normalize-Dir $_) -eq (Normalize-Dir $Dir) }) { return $Current }
    ($parts + $Dir) -join ';'
}

function Merge-McpJson([string]$Json, [string]$Command) {
    # Returns the new JSON text. Keeps other servers and an existing entry's env.
    $cfg = if ($Json -and $Json.Trim()) { $Json | ConvertFrom-Json } else { New-Object psobject }
    if (-not $cfg.PSObject.Properties['mcpServers']) {
        $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object psobject)
    }
    $servers = $cfg.mcpServers
    $servers.PSObject.Properties.Remove($OldServer)
    $entry = $servers.PSObject.Properties[$Server]
    if ($entry) {
        # Update in place so the entry keeps its position and any env or extra keys.
        # Assign when present: Add-Member -Force re-adds the key at the end and reorders the file.
        foreach ($p in @{ command = $Command; args = @('-m', 'autocad_lt_mcp') }.GetEnumerator()) {
            if ($entry.Value.PSObject.Properties[$p.Key]) { $entry.Value.($p.Key) = $p.Value }
            else { $entry.Value | Add-Member -NotePropertyName $p.Key -NotePropertyValue $p.Value }
        }
    } else {
        $servers | Add-Member -NotePropertyName $Server -NotePropertyValue ([pscustomobject][ordered]@{
            command = $Command; args = @('-m', 'autocad_lt_mcp')
            env = [pscustomobject]@{ AUTOCAD_MCP_BACKEND = 'auto' } })
    }
    $cfg | ConvertTo-Json -Depth 10
}

function Get-CodexCommand([string]$Toml, [string]$Name) {
    # The command of [mcp_servers.<Name>], '' if the table has none, $null if there is no table.
    $head = '(?ms)^\[mcp_servers\.("?)' + [regex]::Escape($Name) + '\1\][ \t]*\r?$(.*?)(?=^\[|\z)'
    $m = [regex]::Match($Toml, $head)
    if (-not $m.Success) { return $null }
    $c = [regex]::Match($m.Groups[2].Value, '(?m)^\s*command\s*=\s*[''"](.*?)[''"]')
    if ($c.Success) { $c.Groups[1].Value } else { '' }
}

function New-CodexBlock([string]$Command) {
    "[mcp_servers.$Server]`r`ncommand = '$Command'`r`nargs = [`"-m`", `"autocad_lt_mcp`"]`r`n`r`n" +
    "[mcp_servers.$Server.env]`r`nAUTOCAD_MCP_BACKEND = `"auto`"`r`n"
}

# ---------- machine steps ----------

function Write-Utf8([string]$Path, [string]$Text) {
    # No BOM: Node-based clients can refuse a file that starts with one.
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

function Backup-File([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Copy-Item -LiteralPath $Path (Join-Path $script:BackupDir ((Split-Path $Path -Leaf) + '.bak')) -WhatIf:$false
    }
}

function Backup-RegKey([string]$SubKey, [string]$Name) {
    $file = Join-Path $script:BackupDir "$Name.reg"
    & reg.exe export "HKCU\$SubKey" $file /y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "reg export failed for HKCU\$SubKey" }
}

function Find-App([string]$Name) {
    # .cmd/.exe only: npm's .ps1 shim drops the `--` before the server command.
    Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in '.cmd', '.exe' } | Select-Object -First 1
}

function Get-AutoCADProfileKey {
    # Follow AutoCAD's own pointers: CurVer (release), CurVer (product), Profiles default.
    $root = 'SOFTWARE\Autodesk\AutoCAD LT'
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($root)
    if (-not $k) { return $null }
    $rel = $k.GetValue('CurVer'); if (-not $rel) { return $null }
    $prod = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$root\$rel").GetValue('CurVer')
    if (-not $prod) { return $null }
    $prof = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$root\$rel\$prod\Profiles")
    if (-not $prof -or -not $prof.GetValue('')) { return $null }
    "$root\$rel\$prod\Profiles\$($prof.GetValue(''))"
}

function Get-AutoCADYear {
    $years = Get-ChildItem 'HKLM:\SOFTWARE\Autodesk\AutoCAD LT' -ErrorAction SilentlyContinue |
        Get-ChildItem -ErrorAction SilentlyContinue |
        ForEach-Object { if ((Get-ItemProperty -LiteralPath $_.PSPath).ProductName -match 'AutoCAD LT (\d{4})') { [int]$Matches[1] } }
    ($years | Measure-Object -Maximum).Maximum
}

function Step-StartupSuite([string]$ProfileKey, [string]$LispDir) {
    $sub = "$ProfileKey\Dialogs\Appload\Startup"
    $hk = [Microsoft.Win32.Registry]::CurrentUser
    $k = $hk.OpenSubKey($sub)
    $old = @()
    $numKind = 'String'; $entryKind = 'ExpandString'
    if ($k) {
        $n = [int]("0" + $k.GetValue('NumStartup'))
        if ($k.GetValueNames() -contains 'NumStartup') { $numKind = $k.GetValueKind('NumStartup') }
        if ($k.GetValueNames() -contains '1Startup') { $entryKind = $k.GetValueKind('1Startup') }
        $old = @(1..$n | Where-Object { $n -gt 0 } | ForEach-Object { $k.GetValue("${_}Startup", $null, 'DoNotExpandEnvironmentNames') })
    }
    $new = Merge-StartupList $old $LispDir
    if (($old -join '|') -eq ($new -join '|')) { Write-Host "  Startup Suite already loads $LispDir\mcp_dispatch.lsp."; return }
    $old | Where-Object { $new -notcontains $_ } | ForEach-Object { Write-Host "  remove: $_" }
    $new | Where-Object { $old -notcontains $_ } | ForEach-Object { Write-Host "  add:    $_" }
    if (-not $PSCmdlet.ShouldProcess("HKCU\$sub", 'Rewrite Startup Suite entries')) { return }
    if ($k) { Backup-RegKey $sub 'startup-suite' }
    $w = $hk.CreateSubKey($sub)
    for ($i = 0; $i -lt $new.Count; $i++) { $w.SetValue("$($i + 1)Startup", $new[$i], $entryKind) }
    for ($i = $new.Count + 1; $i -le $old.Count; $i++) { $w.DeleteValue("${i}Startup", $false) }
    $w.SetValue('NumStartup', "$($new.Count)", $numKind)
    $w.Close()
    Write-Host "  Startup Suite updated ($($new.Count) entries)."
}

function Step-TrustedPath([string]$ProfileKey, [string]$LispDir) {
    $sub = "$ProfileKey\Variables"
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($sub)
    $secure = if ($k) { $k.GetValue('SECURELOAD') } else { $null }
    if ($null -eq $secure) { $secure = '1' }  # AutoCAD's default
    $cur = if ($k) { [string]$k.GetValue('TRUSTEDPATHS') } else { '' }
    $new = Add-TrustedPath $cur $LispDir "$secure"
    if ($new -eq $cur) {
        Write-Host $(if ("$secure" -eq '0') { "  SECURELOAD is 0, so no trusted path is needed." } else { "  $LispDir is already trusted." })
        return
    }
    if (-not $PSCmdlet.ShouldProcess("HKCU\$sub TRUSTEDPATHS", "Add $LispDir")) { return }
    if ($k) { Backup-RegKey $sub 'variables' }
    $w = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($sub)
    $w.SetValue('TRUSTEDPATHS', $new, 'String'); $w.Close()
    Write-Host "  Added $LispDir to TRUSTEDPATHS."
}

function Step-Claude([string]$Python) {
    $claude = Find-App 'claude'
    if (-not $claude) { Write-Host "  Claude Code not found; skipped."; return }
    $ErrorActionPreference = 'Continue'  # PS 5.1 turns a native command's stderr into a terminating error under Stop
    Push-Location $env:TEMP  # outside any project, so `get` sees the user-scope entry only
    try {
        $null = & $claude.Source mcp get $OldServer 2>&1
        if ($LASTEXITCODE -eq 0 -and $PSCmdlet.ShouldProcess("Claude Code: $OldServer", 'Remove old entry')) {
            & $claude.Source mcp remove $OldServer -s user 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove '$OldServer' at user scope. Remove it by hand: claude mcp remove $OldServer" }
        }
        $got = (& $claude.Source mcp get $Server 2>&1) -join "`n"
        if ($LASTEXITCODE -eq 0 -and $got.Contains($Python)) { Write-Host "  Claude Code already points at $Python."; return }
        if (-not $PSCmdlet.ShouldProcess("Claude Code: $Server -> $Python", 'Register (user scope)')) { return }
        if ($LASTEXITCODE -eq 0) { & $claude.Source mcp remove $Server -s user 2>&1 | Out-Null }
        & $claude.Source mcp add $Server -s user -e AUTOCAD_MCP_BACKEND=auto -- $Python -m autocad_lt_mcp
        if ($LASTEXITCODE -ne 0) { throw "claude mcp add failed" }
        Write-Host "  Registered with Claude Code."
    } finally { Pop-Location }
}

function Step-Antigravity([string]$Python, [string]$Repo) {
    $dir = Join-Path $HOME '.gemini\config'
    if (-not (Find-App 'agy') -and -not (Test-Path -LiteralPath $dir)) { Write-Host "  Antigravity not found; skipped."; return }
    $path = Join-Path $dir 'mcp_config.json'
    $text = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { '' }
    $before = if ($text.Trim()) { $text | ConvertFrom-Json | ConvertTo-Json -Depth 10 } else { '' }
    $after = Merge-McpJson $text $Python
    if ($after -eq $before) { Write-Host "  Antigravity already points at $Python." }
    elseif ($PSCmdlet.ShouldProcess($path, "Set '$Server' (and drop '$OldServer')")) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Backup-File $path
        Write-Utf8 $path $after
        Write-Host "  Antigravity config updated: $path"
    }
    Copy-Skills (Join-Path $dir 'skills') $Repo
}

function Step-Codex([string]$Python, [string]$Repo) {
    if (-not (Find-App 'codex')) { Write-Host "  Codex not found; skipped."; return }
    $path = Join-Path $HOME '.codex\config.toml'
    $text = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { '' }
    if ($null -ne (Get-CodexCommand $text $OldServer)) {
        Write-Warning "Codex still has [mcp_servers.$OldServer] in $path. Delete that table (and its .env table) by hand."
    }
    $cmd = Get-CodexCommand $text $Server
    if ($null -eq $cmd) {
        if ($PSCmdlet.ShouldProcess($path, "Append [mcp_servers.$Server]")) {
            New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
            Backup-File $path
            $sep = if ($text -and -not $text.EndsWith("`n")) { "`r`n`r`n" } elseif ($text) { "`r`n" } else { '' }
            Write-Utf8 $path ($text + $sep + (New-CodexBlock $Python))
            Write-Host "  Codex config updated: $path"
        }
    } elseif ($cmd -eq $Python) {
        Write-Host "  Codex already points at $Python."
    } else {
        # Edited by hand only: the table may carry settings (approval mode, env) worth keeping.
        Write-Warning "Codex's [mcp_servers.$Server] runs '$cmd'. Change its command to '$Python' in $path."
    }
    Copy-Skills (Join-Path $HOME '.agents\skills') $Repo
}

function Copy-Skills([string]$Dest, [string]$Repo) {
    # Pre-rename copies would load alongside the new ones and give the old rules.
    foreach ($name in $OldSkillNames) {
        $old = Join-Path $Dest $name
        if ((Test-Path -LiteralPath $old) -and $PSCmdlet.ShouldProcess($old, 'Remove pre-rename skill')) {
            Remove-Item -LiteralPath $old -Recurse -Force
            Write-Host "  Removed old skill $old"
        }
    }
    foreach ($name in $SkillNames) {
        $target = Join-Path $Dest $name
        if ($PSCmdlet.ShouldProcess($target, 'Copy skill')) {
            New-Item -ItemType Directory -Force -Path $Dest | Out-Null
            # Remove first: Copy-Item into an existing folder nests a second copy inside it.
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
            Copy-Item -LiteralPath (Join-Path $Repo "skills\$name") $target -Recurse
        }
    }
    Write-Host "  Skills: $Dest"
}

function Invoke-Install {
    $ErrorActionPreference = 'Stop'
    $repo = Split-Path -Parent $PSScriptRoot
    $lisp = Join-Path $repo 'lisp-code'
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    $script:BackupDir = Join-Path $env:LOCALAPPDATA ("autocad-lt-mcp\backups\" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Write-Host "Installing autocad-lt-mcp from $repo"

    Write-Host "`n[1/5] Prerequisites"
    $missing = @()
    if (-not (Find-App 'uv')) { $missing += 'uv: https://docs.astral.sh/uv/getting-started/installation/' }
    if (-not (Find-App 'git')) { $missing += 'git: https://git-scm.com/download/win' }
    $year = Get-AutoCADYear
    if (-not $year -or $year -lt 2024) { $missing += "AutoCAD LT 2024 or newer (found: $(if ($year) { $year } else { 'none' }))" }
    $profileKey = Get-AutoCADProfileKey
    if ($year -and -not $profileKey) { $missing += 'An AutoCAD profile: start AutoCAD LT once, close it, then run this again.' }
    if ($missing) { $missing | ForEach-Object { Write-Host "  MISSING  $_" }; throw 'Install the items above, then run this again.' }
    $running = Get-Process -Name acadlt, acad -ErrorAction SilentlyContinue
    if ($running) {
        if (-not $WhatIfPreference) { throw 'AutoCAD is running. Close it (it writes its profile on exit and would undo the Startup Suite change), then run this again.' }
        Write-Warning 'AutoCAD is running; a real run would stop here.'
    }
    Write-Host "  uv, git, AutoCAD LT $year found. Profile: HKCU\$profileKey"
    if (-not $WhatIfPreference) { New-Item -ItemType Directory -Force -Path $script:BackupDir | Out-Null }

    Write-Host "`n[2/5] uv sync"
    if ($PSCmdlet.ShouldProcess($repo, 'uv sync')) {
        Push-Location $repo
        try { & uv sync; if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' } } finally { Pop-Location }
    }

    Write-Host "`n[3/5] AutoCAD Startup Suite and trusted paths"
    Step-StartupSuite $profileKey $lisp
    Step-TrustedPath $profileKey $lisp

    Write-Host "`n[4/5] Assistants"
    Step-Claude $python
    Step-Antigravity $python $repo
    Step-Codex $python $repo

    Write-Host "`n[5/5] Skill zips for claude.ai"
    if ($PSCmdlet.ShouldProcess((Join-Path $repo 'dist'), 'Build skill zips')) {
        & (Join-Path $PSScriptRoot 'sync-skills.ps1') -Package  # prints each zip's full path
    }

    Write-Host "`nDone. Left for you:"
    Write-Host "  - Upload each zip above in claude.ai: Customize, Skills (replace older copies)."
    Write-Host "  - Restart your assistant so it picks up the server."
    Write-Host "  - Open AutoCAD, open or start a drawing, and ask the assistant to run system(operation=`"init`")."
    Write-Host "    It should report backend: file_ipc."
    if (Test-Path -LiteralPath $script:BackupDir) { Write-Host "Backups (if anything changed): $script:BackupDir" }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Install }
