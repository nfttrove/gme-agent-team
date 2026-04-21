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

ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
gemma_local = LLM(
    model="ollama/gemma2:9b",
    base_url=ollama_host,
    temperature=0.1,
)
