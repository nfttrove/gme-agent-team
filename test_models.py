import os
from dotenv import load_dotenv
from crewai import LLM

load_dotenv()

def test_ollama_gemma():
    print("Testing Ollama (gemma2:9b) — local, free...")
    llm = LLM(model="ollama/gemma2:9b", base_url="http://localhost:11434", temperature=0)
    response = llm.call([{"role": "user", "content": "Reply with only: GEMMA_OK"}])
    assert "GEMMA" in response.upper() or "OK" in response.upper(), f"Unexpected: {response}"
    print(f"  PASS — got: {response.strip()}")

def test_gemini():
    print("Testing Gemini API — cloud, pay-per-use...")
    key = os.getenv("GOOGLE_API_KEY")
    assert key and key != "your_key_here", "GOOGLE_API_KEY not set in .env"
    llm = LLM(
        model="gemini/gemini-2.5-flash",
        api_key=key,
        temperature=0,
    )
    try:
        response = llm.call([{"role": "user", "content": "Reply with only: GEMINI_OK"}])
        assert "GEMINI" in response.upper() or "OK" in response.upper(), f"Unexpected: {response}"
        print(f"  PASS — got: {response.strip()}")
        return True
    except Exception as e:
        if "quota" in str(e).lower() or "rate" in str(e).lower():
            print("  SKIP — Gemini quota exceeded. Try again later.")
        else:
            print(f"  FAIL — {e}")
        return False

if __name__ == "__main__":
    ollama_ok = True
    gemini_ok = False

    test_ollama_gemma()
    gemini_ok = test_gemini()

    print()
    if gemini_ok:
        print("Both models ready. main.py will use Gemma for Analyst/Strategist and Gemini for Manager.")
    else:
        print("Only Ollama is ready. To run fully local, Manager can also use Gemma (free, no account needed).")
        print("To switch: open main.py and change  MANAGER_MODEL = gemini_llm  to  MANAGER_MODEL = ollama_llm")
