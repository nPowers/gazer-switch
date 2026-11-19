# Gazer installer for Windows PowerShell 5.1 or newer. ASCII only.
# Double-click install.bat. It runs this script with -ExecutionPolicy Bypass.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Get-PythonInfo($Exe, $Prefix) {
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $prefix = @($Prefix)
    $code = "import sys,struct; print(sys.version.split()[0]); print(struct.calcsize('P')*8); print(sys.executable)"
    try {
        $raw = & $Exe @prefix -c $code 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    $lines = @()
    foreach ($line in @($raw)) {
        $t = ("$line").Trim()
        if ($t.Length -gt 0) { $lines += $t }
    }
    if ($lines.Count -lt 3) { return $null }
    $version = $lines[0]
    $bits = 0
    if (-not [int]::TryParse($lines[1], [ref]$bits)) { return $null }
    $parts = $version.Split(".")
    if ($parts.Count -lt 2) { return $null }
    $major = 0
    $minor = 0
    if (-not [int]::TryParse($parts[0], [ref]$major)) { return $null }
    if (-not [int]::TryParse($parts[1], [ref]$minor)) { return $null }
    $ok = ($major -eq 3 -and $minor -ge 12 -and $minor -le 13 -and $bits -eq 64)
    return @{
        Exe = $Exe
        Prefix = $prefix
        Version = $version
        Bits = $bits
        Major = $major
        Minor = $minor
        Executable = $lines[2]
        Ok = $ok
    }
}

function Find-GazerPython {
    $preferred = @(
        @{ Exe = "py"; Prefix = @("-3.13") },
        @{ Exe = "py"; Prefix = @("-3.12") },
        @{ Exe = "python"; Prefix = @() }
    )
    foreach ($item in $preferred) {
        $info = Get-PythonInfo $item.Exe $item.Prefix
        if ($null -ne $info -and $info.Ok) { return $info }
    }
    return $null
}

function Get-UnsuitablePythonNotes {
    $probes = @(
        @{ Exe = "py"; Prefix = @("-3.13") },
        @{ Exe = "py"; Prefix = @("-3.12") },
        @{ Exe = "python"; Prefix = @() },
        @{ Exe = "py"; Prefix = @() }
    )
    $notes = @()
    $seen = @{}
    foreach ($item in $probes) {
        $info = Get-PythonInfo $item.Exe $item.Prefix
        if ($null -eq $info) { continue }
        if ($info.Ok) { continue }
        $key = ("{0}|{1}" -f $info.Executable, $info.Version)
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $notes += $info
    }
    return ,$notes
}

function Read-JsonFile($Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Length -gt 0 -and [int][char]$text[0] -eq 65279) {
        $text = $text.Substring(1)
    }
    return ($text | ConvertFrom-Json)
}

function Read-Choice($Prompt, $Default) {
    $text = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($text)) { return "$Default" }
    return $text.Trim()
}

function Read-ComPort($Default) {
    while ($true) {
        $text = Read-Choice "COM port for the USB switch" $Default
        $text = $text.ToUpper()
        if ($text -match "^COM[0-9]+$") { return $text }
        Write-Host "Enter a port like COM3."
    }
}

function Read-Camera($Default) {
    while ($true) {
        Write-Host "Enter a camera name, or a number 0-10 to use that index."
        $text = Read-Choice "Camera" $Default
        if ($text -match "^[0-9]+$") {
            $index = [int]$text
            if ($index -ge 0 -and $index -le 10) { return $text }
            Write-Host "Camera index must be 0 through 10."
            continue
        }
        if ($text.Length -gt 0) { return $text }
        Write-Host "Enter a camera name or an index."
    }
}

function Read-SwitchPort($Prompt, $Default, $Other) {
    while ($true) {
        $text = Read-Choice $Prompt $Default
        $number = 0
        $ok = [int]::TryParse($text, [ref]$number)
        if ((-not $ok) -or $number -lt 1 -or $number -gt 4) {
            Write-Host "Enter a switch port from 1 to 4."
            continue
        }
        if ($null -ne $Other -and $number -eq [int]$Other) {
            Write-Host "Left and right must be different ports."
            continue
        }
        return $number
    }
}

Write-Host "Gazer setup"
Write-Host "Looking for 64-bit Python 3.12 or 3.13 ..."
$python = Find-GazerPython
if ($null -eq $python) {
    Write-Host ""
    Write-Host "Gazer needs 64-bit Python 3.12 or 3.13"
    $found = @(Get-UnsuitablePythonNotes)
    if ($found.Count -gt 0) {
        foreach ($item in $found) {
            Write-Host "Found Python $($item.Version) ($($item.Bits)-bit), which cannot be used."
        }
    } else {
        Write-Host "64-bit Python 3.12 or 3.13 was not found."
    }
    Write-Host "Download it from https://www.python.org/downloads/windows/"
    Write-Host "In the installer, turn on 'Add python.exe to PATH'."
    Write-Host "Or, if you use winget: winget install Python.Python.3.13"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        $answer = Read-Host "Install Python 3.13 with winget now? [y/N]"
        if ($answer -eq "y" -or $answer -eq "Y") {
            & winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements
            Write-Host ""
            Write-Host "If that install succeeded, close this window and run install.bat again."
            Write-Host "A new window is required so PATH includes Python."
            exit 1
        }
    }
    Write-Host "Stopped. Install 64-bit Python 3.12 or 3.13, then run install.bat again."
    exit 1
}
Write-Host "Using Python $($python.Version) ($($python.Exe) $($python.Prefix -join ' '))"

$venvDir = Join-Path $root ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "Creating .venv ..."
    & $python.Exe @($python.Prefix) -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPy)) {
        Write-Host "Could not create .venv."
        exit 1
    }
} else {
    Write-Host "Using existing .venv"
}

Write-Host "Upgrading pip ..."
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip upgrade failed. If this .venv is broken, delete the .venv folder and run install.bat again."
    exit 1
}
Write-Host "Installing requirements ..."
& $venvPy -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing requirements failed."
    exit 1
}

$configPath = Join-Path $root "config.json"
$examplePath = Join-Path $root "config.example.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    if (-not (Test-Path -LiteralPath $examplePath)) {
        Write-Host "config.example.json is missing, so config.json was not created."
        exit 1
    }
    try {
        [System.IO.File]::Copy($examplePath, $configPath, $false)
        Write-Host "Created config.json from config.example.json"
    } catch {
        if (Test-Path -LiteralPath $configPath) {
            Write-Host "config.json already exists. Saved calibration will be kept."
        } else {
            throw
        }
    }
} else {
    Write-Host "config.json already exists. Saved calibration will be kept."
}

$cfg = Read-JsonFile $configPath
$portDefault = "COM3"
if ($cfg.com_port) { $portDefault = [string]$cfg.com_port }
$camDefault = "0"
if ($cfg.camera_name -and -not [string]::IsNullOrWhiteSpace([string]$cfg.camera_name)) {
    $camDefault = [string]$cfg.camera_name
} elseif ($null -ne $cfg.camera_index) {
    $camDefault = [string]$cfg.camera_index
}
$leftDefault = 1
if ($null -ne $cfg.left_host) { $leftDefault = [int]$cfg.left_host }
$rightDefault = 2
if ($null -ne $cfg.right_host) { $rightDefault = [int]$cfg.right_host }

Write-Host ""
Write-Host "Serial ports:"
& $venvPy (Join-Path $root "gaze_switch.py") --list-ports
if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not list serial ports. Type the COM port if you know it."
}
Write-Host ""
$port = Read-ComPort $portDefault

Write-Host ""
Write-Host "Cameras:"
& $venvPy (Join-Path $root "gaze_switch.py") --list-cameras
if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not list cameras. Type a name or an index if you know it."
}
Write-Host ""
$camera = Read-Camera $camDefault

Write-Host ""
Write-Host "Which switch port is each monitor on? Press Enter to keep the value in brackets."
$left = Read-SwitchPort "Switch port for the LEFT monitor (1-4)" $leftDefault $null
$right = Read-SwitchPort "Switch port for the RIGHT monitor (1-4)" $rightDefault $left

$updater = @'
import sys
import gaze_switch
changed = gaze_switch.write_setup_choices(
    sys.argv[1],
    com_port=sys.argv[2],
    camera=sys.argv[3],
    left_host=int(sys.argv[4]),
    right_host=int(sys.argv[5]),
)
print("updated" if changed else "unchanged")
'@
& $venvPy -c $updater $configPath $port $camera "$left" "$right"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not save those choices. config.json was left as it was."
    exit 1
}

Write-Host ""
Write-Host "Running gaze_switch.py --check ..."
& $venvPy (Join-Path $root "gaze_switch.py") --check
if ($LASTEXITCODE -ne 0) {
    Write-Host "--check failed. Gazer is not ready to run yet."
    exit 1
}

$startup = [Environment]::GetFolderPath("Startup")
$legacy = Join-Path $startup "eKL gaze switch.lnk"
$shortcut = Join-Path $startup "Gazer.lnk"
$target = Join-Path $root "start_minimized.bat"
Write-Host ""
if (Test-Path -LiteralPath $legacy) {
    Write-Host "A Startup shortcut named 'eKL gaze switch' is already here. Leaving it in place."
    Write-Host "Not creating a second shortcut."
} else {
    $answer = Read-Host "Start Gazer when you sign in to Windows? [y/N]"
    if ($answer -eq "y" -or $answer -eq "Y") {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($shortcut)
        $link.TargetPath = $target
        $link.WorkingDirectory = $root
        $link.WindowStyle = 7
        $link.Description = "Gazer"
        $link.Save()
        Write-Host "Startup shortcut created. It starts minimized and disarmed."
    } else {
        if (Test-Path -LiteralPath $shortcut) {
            Remove-Item -LiteralPath $shortcut -Force
            Write-Host "Removed the Gazer Startup shortcut."
        } else {
            Write-Host "Gazer will not start at sign-in."
        }
    }
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Double-click run.bat to open the preview window."
Write-Host "  2. Click the window so it has the keyboard."
Write-Host "  3. Press C, look at the left monitor, press C, and hold still for one second."
Write-Host "  4. Look at the right monitor, press C, and hold still for one second."
Write-Host "  5. Press 1 or 2 if you need to tell Gazer which computer is active now."
Write-Host "  6. Press Space to arm. The tray icon turns green."
Write-Host "  7. Closing the preview hides it. Quit is on the tray icon, or press Q."
Write-Host ""
Write-Host "The tray icon sits by the clock: grey paused, green running, amber error."
Write-Host "Right-click it for Show, Hide, Pause or Resume, Calibrate, Relaunch, and Quit."
Write-Host "Gazer starts disarmed every time. Nothing switches until you arm it."
Write-Host "A second launch shows the one that is already running."
exit 0
