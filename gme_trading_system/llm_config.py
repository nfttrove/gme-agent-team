import os
from crewai import LLM
from dotenv import load_dotenv

load_dotenv()

gemini_pro = LLM(
    model="gemini/gemini-2.5-pro",
    api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0.3,
)

gemini_flash = LLM(
    model="gemini/gemini-2.5-flash",
    api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0,
)

gemma_local = LLM(
    model="ollama/gemma2:9b",
    base_url="http://localhost:11434",
    temperature=0.1,
)
