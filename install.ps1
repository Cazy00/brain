# brain — one-command install for Windows.
#
#   irm https://raw.githubusercontent.com/Cazy00/brain/main/install.ps1 | iex
#
# A bootstrap and nothing more: check that git and python 3.9+ are present,
# fetch the template into a temp directory, and hand off to `brain setup` —
# the same code path macOS and Linux take through install.sh. Anything
# interactive lives there, in Python, so all three platforms behave identically
# and none of them has a second implementation to keep in step.
#
# It does NOT wire your agents. `brain init` re-points the global
# ~/.claude/skills/brain link at whatever checkout ran it, and this script runs
# from a clone it is about to delete. Wiring is `brain connect`, run afterwards
# from the brain itself.
#
# Stated risk: Windows is verified by CI only. Nobody on this project has a
# Windows machine, so anything CI cannot reach — how the path picker feels in a
# real terminal, Credential Manager prompts — is unverified rather than known
# to work.

$ErrorActionPreference = "Stop"

function Need($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "$name is required. Install it with:  $hint"
        exit 1
    }
}

# Never installed for you, on any platform: a piped script that installs system
# packages unprompted assumes more trust than this should, and corporate
# machines forbid it outright. The command is printed; running it is your call.
Need git    "winget install --id Git.Git"
Need python "winget install --id Python.Python.3.12"

$floor = & python -c "import sys; print(1 if sys.version_info[:2] >= (3,9) else 0)"
if ($floor.Trim() -ne "1") {
    Write-Error "python 3.9 or newer is required (found $(& python -V))."
    exit 1
}

$repo = if ($env:BRAIN_TEMPLATE_REPO) { $env:BRAIN_TEMPLATE_REPO } else { "Cazy00/brain" }
$temp = Join-Path $env:TEMP ("brain-install-" + [System.Guid]::NewGuid().ToString("N"))

Write-Host ""
Write-Host "brain — a second brain that stays true"
Write-Host "installing from $repo"
Write-Host ""

git clone --quiet --depth 1 "https://github.com/$repo.git" $temp
if ($LASTEXITCODE -ne 0) {
    Write-Error "could not clone https://github.com/$repo.git"
    exit 1
}

try {
    # setup's exit code is doctor's verdict, so it is passed straight out
    # rather than reduced to success/failure here.
    & python (Join-Path $temp "bin\brain") setup @args
    exit $LASTEXITCODE
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
