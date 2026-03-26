# restart_bot.ps1 - Kill trading_bot\bot.py only, then restart it.
# Does NOT touch receipt_bot, attachment_bot, relay, antiraid_bot, or webhook bots.

$pythonExe = "C:\Users\User\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$botDir    = "C:\Users\User\Desktop\trading_bot"
$botScript = "bot.py"

# Step 1: Find all python processes running trading_bot\bot.py
Write-Host "Looking for trading_bot\bot.py processes..."
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
         Where-Object { $_.CommandLine -like '*trading_bot*bot.py*' }

if ($procs) {
    foreach ($p in @($procs)) {
        Write-Host "Killing PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Done killing."
} else {
    Write-Host "No running trading_bot\bot.py process found."
}

# Step 2: Wait
Start-Sleep -Seconds 2

# Step 3: Start the bot hidden
Write-Host "Starting $botScript..."
Start-Process `
    -FilePath $pythonExe `
    -ArgumentList $botScript `
    -WorkingDirectory $botDir `
    -WindowStyle Hidden

# Step 4: Verify
Start-Sleep -Seconds 3

$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
           Where-Object { $_.CommandLine -like '*trading_bot*bot.py*' }

if ($running) {
    foreach ($r in @($running)) {
        Write-Host "RUNNING: PID $($r.ProcessId)"
    }
} else {
    Write-Host "WARNING: could not verify trading bot is running. Check logs."
}
