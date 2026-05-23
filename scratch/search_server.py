import sys

def main():
    content = open('src/agents/server.py', encoding='utf-8').read()
    lines = content.splitlines()
    # Print lines 836 to 920 of server.py
    sys.stdout.reconfigure(encoding='utf-8')
    for idx in range(835, 930):
        if idx < len(lines):
            print(f"{idx+1}: {lines[idx]}")

if __name__ == '__main__':
    main()
