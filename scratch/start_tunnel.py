import subprocess
import time
import os
import sys

def main():
    print("Running diagnostic tunnel test...")
    repo_dir = r"C:\Users\eourn\.gemini\antigravity\scratch\repo\Agent-OS-Final-Pro-main"
    cloudflared_path = os.path.join(repo_dir, "cloudflared.exe")
    
    cmd = [
        cloudflared_path,
        "tunnel",
        "--url",
        "http://localhost:8001"
    ]
    
    # Start process with stderr piped
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
    )
    
    # Wait for 15 seconds to let it connect and print its output
    time.sleep(15)
    
    # Terminate process
    proc.terminate()
    stdout, stderr = proc.communicate()
    
    print("\n--- STDOUT ---")
    print(stdout)
    print("\n--- STDERR ---")
    print(stderr)

if __name__ == "__main__":
    main()
