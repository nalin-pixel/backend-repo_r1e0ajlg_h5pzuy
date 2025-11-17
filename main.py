import os
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from bson.objectid import ObjectId
from hashlib import sha256

from database import db, create_document, get_documents

# LLM client (optional, falls back if not configured)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        openai_client = OpenAI()
except Exception:
    openai_client = None

app = FastAPI(title="EduSense API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------- Helpers -------------------

def hash_password(password: str) -> str:
    return sha256(password.encode()).hexdigest()


def to_public(doc):
    if not doc:
        return doc
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    return d


def get_recent_chat_history(user_id: str, limit: int = 8):
    try:
        cursor = db["chatmessage"].find({"user_id": user_id}).sort("created_at", -1).limit(limit)
        items = list(cursor)
        items.reverse()  # oldest first for chat completion
        history = []
        for it in items:
            role = it.get("role", "user")
            content = it.get("content", "")
            history.append({"role": role, "content": content})
        return history
    except Exception:
        return []


# ------------------- Auth -------------------
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/auth/register")
async def register(payload: RegisterRequest):
    # check existing
    exists = list(db["user"].find({"email": payload.email}))
    if exists:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = create_document("user", {
        "name": payload.name,
        "email": payload.email,
        "password_hash": hash_password(payload.password),
        "avatar_url": None,
    })
    return {"user_id": user_id}

@app.post("/auth/login")
async def login(payload: LoginRequest):
    user = db["user"].find_one({"email": payload.email})
    if not user or user.get("password_hash") != hash_password(payload.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"user_id": str(user["_id"]), "name": user.get("name"), "email": user.get("email")}

# ------------------- Materials -------------------
class MaterialCreate(BaseModel):
    user_id: str
    title: str
    subject: Optional[str] = None
    content: str

@app.post("/materials")
async def create_material(payload: MaterialCreate):
    mat_id = create_document("material", {
        "user_id": payload.user_id,
        "title": payload.title,
        "subject": payload.subject,
        "content": payload.content,
        "difficulty": "normal",
    })
    return {"material_id": mat_id}

@app.get("/materials/{user_id}")
async def list_materials(user_id: str):
    docs = get_documents("material", {"user_id": user_id})
    return [to_public(d) for d in docs]

# ------------------- Videos -------------------
class VideoCreate(BaseModel):
    user_id: str
    title: str
    subject: Optional[str] = None
    url: str

@app.post("/videos")
async def create_video(payload: VideoCreate):
    vid = create_document("video", payload.model_dump())
    return {"video_id": vid}

@app.get("/videos/{user_id}")
async def list_videos(user_id: str):
    docs = get_documents("video", {"user_id": user_id})
    return [to_public(d) for d in docs]

# ------------------- Emotion logs -------------------
class EmotionLogCreate(BaseModel):
    user_id: str
    emotion: str
    note: Optional[str] = None

@app.post("/emotions")
async def log_emotion(payload: EmotionLogCreate):
    log_id = create_document("emotionlog", payload.model_dump())
    return {"log_id": log_id}

@app.get("/emotions/summary/{user_id}")
async def emotion_summary(user_id: str):
    logs = get_documents("emotionlog", {"user_id": user_id})
    # Simple frequency summary
    freq = {}
    for l in logs:
        e = l.get("emotion", "neutral")
        freq[e] = freq.get(e, 0) + 1
    total = sum(freq.values()) or 1
    growth = {k: v/total for k, v in freq.items()}
    return {"frequency": freq, "distribution": growth, "total": total}

# ------------------- Content adaptation -------------------
class AdaptRequest(BaseModel):
    user_id: str
    material_id: Optional[str] = None
    latest_emotion: str

@app.post("/adapt")
async def adapt_content(payload: AdaptRequest):
    # Rules based on emotion
    mapping = {
        "sad": {"strategy": "Make it playful", "difficulty": "normal", "activities": ["puzzle", "flashcards"]},
        "confused": {"strategy": "Simplify & add examples", "difficulty": "easy"},
        "angry": {"strategy": "Calm & simplify", "difficulty": "easy"},
        "happy": {"strategy": "Challenge more", "difficulty": "hard"},
        "neutral": {"strategy": "Keep steady", "difficulty": "normal"},
    }
    rule = mapping.get(payload.latest_emotion, mapping["neutral"])

    material = None
    if payload.material_id:
        try:
            material = db["material"].find_one({"_id": ObjectId(payload.material_id)})
        except Exception:
            material = None

    response = {
        "policy": rule,
        "material": to_public(material) if material else None
    }

    # If material present, return a modified suggestion header (simple demo)
    if material:
        header = ""  # computed guidance prefix
        if rule["difficulty"] == "easy":
            header = "Step-by-step explanation: "
        elif rule["difficulty"] == "hard":
            header = "Advanced challenge: "
        else:
            header = "Interactive mode: " if "activities" in rule else "Focus mode: "
        response["suggested_intro"] = header

    return response

# ------------------- Chatbot (LLM-powered with graceful fallback) -------------------
class ChatMessageIn(BaseModel):
    user_id: str
    message: str
    emotion_hint: Optional[str] = None

SYSTEM_PROMPT = (
    "You are EduSense, an emotion-aware learning assistant. "
    "Your goal is to teach clearly and adaptively. "
    "Tone rules by emotion: sad=gently encouraging; confused=clear step-by-step with examples; "
    "angry=calm and concise; happy=enthusiastic and challenging; neutral=friendly and helpful. "
    "Always be supportive, focus on pedagogy (Socratic questions, bite-sized steps), and avoid long tangents."
)

@app.post("/chat")
async def chat_with_assistant(payload: ChatMessageIn):
    # store user message first
    create_document("chatmessage", {"user_id": payload.user_id, "role": "user", "content": payload.message, "emotion_context": payload.emotion_hint})

    def simple_fallback() -> str:
        tone_map = {
            "sad": "gentle and encouraging",
            "confused": "clear and step-by-step",
            "angry": "calm and concise",
            "happy": "enthusiastic and challenging",
            None: "friendly and helpful",
        }
        tone = tone_map.get(payload.emotion_hint, tone_map[None])
        return f"In a {tone} tone: I hear you said: '{payload.message}'. Let's work through this together."

    reply = None

    if openai_client is None:
        reply = simple_fallback()
    else:
        try:
            # Build chat history
            history = get_recent_chat_history(payload.user_id, limit=8)
            # Insert system + emotion instruction
            system_prefix = SYSTEM_PROMPT
            if payload.emotion_hint:
                system_prefix += f" Current learner emotional state: {payload.emotion_hint}. Adjust tone accordingly."

            messages = [{"role": "system", "content": system_prefix}] + history + [
                {"role": "user", "content": payload.message}
            ]

            # Call OpenAI
            completion = openai_client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=messages,
                temperature=0.7,
                max_tokens=400,
            )
            reply = completion.choices[0].message.content.strip()
        except Exception as e:
            reply = simple_fallback()

    # store assistant reply
    create_document("chatmessage", {"user_id": payload.user_id, "role": "assistant", "content": reply, "emotion_context": payload.emotion_hint})
    return {"reply": reply}

# ------------------- Health -------------------
@app.get("/")
def read_root():
    return {"message": "EduSense backend running"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
                response["connection_status"] = "Connected"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    return response

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
