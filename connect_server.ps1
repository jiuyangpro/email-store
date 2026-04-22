param (
    [string]$Command = "ls -la"
)

# 服务器信息
$server = "172.245.196.85"
$port = "22"
$username = "root"
$password = "K9QhjK50zS13tkLx2E"

# 创建密码安全字符串
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force

# 创建凭据对象
$credential = New-Object System.Management.Automation.PSCredential ($username, $securePassword)

# 连接服务器并执行命令
Write-Host "连接到服务器 $server:$port..."
ssh -l $username -p $port $server $Command
