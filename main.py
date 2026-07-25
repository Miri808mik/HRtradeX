import time
import sys

def main():
    print("Highrise test bot started!", flush=True)

    while True:
        print("Bot is alive...", flush=True)
        time.sleep(60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot stopped.", flush=True)
        sys.exit(0)
