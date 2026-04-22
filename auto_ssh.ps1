function Invoke-SSHCommand {
    param (
        [string]$Command
    )

    # Server information
    $server = "172.245.196.85"
    $port = "22"
    $username = "root"
    $password = "K9QhjK50zS13tkLx2E"

    # Create a temporary script file
    $tempScript = New-TemporaryFile | Rename-Item -NewName { $_.BaseName + ".vbs" } -PassThru

    # Write VBS script to automatically input password
    $vbsContent = @"
Set WshShell = WScript.CreateObject("WScript.Shell")
WScript.Sleep 1000
WshShell.SendKeys "$password{ENTER}"
WScript.Sleep 1000
WshShell.SendKeys "{ENTER}"
"@
    Set-Content -Path $tempScript.FullName -Value $vbsContent

    # Start VBS script
    Start-Process -FilePath "cscript.exe" -ArgumentList "//nologo", $tempScript.FullName

    # Execute SSH command
    Write-Host "Executing command: $Command"
    ssh -l $username -p $port $server $Command

    # Wait for command to complete
    Start-Sleep -Seconds 2

    # Remove temporary script file
    Remove-Item -Path $tempScript.FullName -Force
}

# Export function
export-modulemember -function Invoke-SSHCommand
