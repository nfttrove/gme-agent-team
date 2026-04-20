import os
from crewai import LLM
from dotenv import load_dotenv

load_dotenv()

deepseek_v3 = LLM(
    model="deepseek/deepseek-chat",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1",
    temperature=0.3,
)

deepseek_r1 = LLM(
    model="deepseek/deepseek-reasoner",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1",
    temperature=0.2,
)

gemini_pro = LLM(
    model="gemini/gemini-2.5-pro",
    api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0.3,
)

gemini_flash = LLM(
    model="gemini/gemini-2.5-flash",
    api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0.2,
)

gemma_local = LLM(
    model="ollama/gemma2:9b",
    base_url="http://localhost:11434",
    temperature=0.1,
)
