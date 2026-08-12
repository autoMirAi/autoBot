$ErrorActionPreference = "Continue"

$workerRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "D:\comfyui\ComfyUI_windows_portable\python_embeded\python.exe"
$worker = Join-Path $workerRoot "comfyui_worker.py"
$log = Join-Path $workerRoot "worker.log"

while ($true) {
    & $python $worker *>> $log
    Start-Sleep -Seconds 10
}
