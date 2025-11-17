"""
Database Schemas for EduSense

Each Pydantic model maps to a MongoDB collection (lowercased class name).
"""
from pydantic import BaseModel, Field
from typing import Optional, List

class User(BaseModel):
    name: str = Field(..., description="Full name")
    email: str = Field(..., description="Email address")
    password_hash: str = Field(..., description="Hashed password")
    avatar_url: Optional[str] = Field(None)

class Material(BaseModel):
    user_id: str = Field(...)
    title: str = Field(...)
    subject: Optional[str] = Field(None)
    content: str = Field(..., description="Raw text content of study material")
    difficulty: str = Field("normal", description="easy|normal|hard")

class Video(BaseModel):
    user_id: str = Field(...)
    title: str = Field(...)
    subject: Optional[str] = Field(None)
    url: str = Field(..., description="Video URL (e.g., YouTube)")

class EmotionLog(BaseModel):
    user_id: str
    emotion: str = Field(..., description="happy|sad|angry|confused|neutral")
    note: Optional[str] = None

class ChatMessage(BaseModel):
    user_id: str
    role: str = Field(..., description="user|assistant")
    content: str
    emotion_context: Optional[str] = None

# The viewer may read these via /schema
