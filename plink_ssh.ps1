param (
    [string]$Command = "ls -la"
)

# Server information
$server = "172.245.196.85"
$port = "22"
$username = "root"
$password = "K9QhjK50zS13tkLx2E"

# Check if plink.exe exists
$plinkPath = "$env:ProgramFiles\PuTTY\plink.exe"
if (!(Test-Path $plinkPath)) {
    $plinkPath = "$env:ProgramFiles (x86)\PuTTY\plink.exe"
    if (!(Test-Path $plinkPath)) {
        Write-Host "Error: plink.exe not found. Please install PuTTY." -ForegroundColor Red
        exit 1
    }
}

# Execute command using plink
Write-Host "Executing command: $Command"
& "$plinkPath" -ssh -P $port -l $username -pw $password $server $Command
