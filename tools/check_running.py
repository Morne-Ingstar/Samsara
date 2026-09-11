import subprocess
result = subprocess.run(['tasklist', '/v', '/fi', 'imagename eq python.exe', '/fo', 'csv'], capture_output=True, text=True)
for line in result.stdout.strip().split('\n'):
    if 'dictation' in line.lower() or 'samsara' in line.lower() or 'python' in line.lower():
        print(line[:200])
