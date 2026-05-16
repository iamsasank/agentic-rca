"""Entry point — delegates to worker.py."""
from worker import main
from dotenv import load_dotenv
import asyncio

load_dotenv()  

if __name__ == "__main__":
    asyncio.run(main())
