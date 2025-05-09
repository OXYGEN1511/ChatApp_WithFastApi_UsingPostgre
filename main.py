from fastapi import FastAPI, Form, Request, Query, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import random
import time
import logging
from fastapi import status
from db import get_db, PostgresDB
import socketio
from contextlib import closing
import traceback

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development only, restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables
connected_users = {}
user_sessions = {}

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Setup Socket.IO
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio)
app.mount('/socket.io', socket_app)

# Initialize database connection at startup
@app.on_event("startup")
async def startup_db_client():
    try:
        PostgresDB.initialize()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        # Don't raise here to allow the app to start even if DB is not available

@app.on_event("shutdown")
async def shutdown_db_client():
    # No explicit shutdown needed for this pool implementation
    logger.info("Application shutting down")

@sio.event
async def connect(sid):
    logger.info(f"Client connected: {sid}")

@sio.event
async def authenticate(sid, data):
    if 'mobile' in data and 'session_id' in data:
        mobile = data['mobile']
        session_id = data['session_id']
        
        if session_id in user_sessions and user_sessions[session_id].get("mobile") == mobile:
            connected_users[mobile] = sid
            await sio.emit('authenticated', {'status': 'success', 'mobile': mobile}, room=sid)
            logger.info(f"User {mobile} authenticated with socket {sid}")
            return True
    
    await sio.emit('authenticated', {'status': 'failed'}, room=sid)
    return False

@sio.event
async def disconnect(sid):
    user_to_remove = None
    for mobile, socket_id in connected_users.items():
        if socket_id == sid:
            user_to_remove = mobile
            break
    
    if user_to_remove:
        del connected_users[user_to_remove]
        logger.info(f"User {user_to_remove} disconnected")
    
    logger.info(f"Client disconnected: {sid}")

@sio.event
async def join_chat(sid, data):
    if 'chat_id' in data:
        chat_id = data['chat_id']
        await sio.enter_room(sid, chat_id)
        logger.info(f"User {sid} joined chat room: {chat_id}")

@sio.event
async def leave_chat(sid, data):
    if 'chat_id' in data:
        chat_id = data['chat_id']
        await sio.leave_room(sid, chat_id)
        logger.info(f"User {sid} left chat room: {chat_id}")


def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id and session_id in user_sessions and user_sessions[session_id].get("verified"):
        return user_sessions[session_id]["mobile"]
    return None

def get_chat_id(user1: str, user2: str):
    users = sorted([user1, user2])
    return f"{users[0]}:{users[1]}"


@app.get("/", response_class=HTMLResponse)
async def login_page():
    try:
        with open("static/login.html", "r") as f:
            return HTMLResponse(content=f.read())
    except Exception as e:
        logger.error(f"Failed to read login page: {str(e)}")
        return HTMLResponse(content="<html><body>Error loading login page. Please check server logs.</body></html>")

@app.post("/login-or-register")
async def login_or_register(mobile: str = Form(...)):
    otp = str(random.randint(1000, 9999))
    logger.info(f"Processing login/register for mobile: {mobile} with OTP: {otp}")
    
    try:
           with get_db() as cursor:
            cursor.execute("SELECT id FROM users WHERE mobile = %s", (mobile,))
            user = cursor.fetchone()

            if user:
                cursor.execute("UPDATE users SET otp = %s WHERE mobile = %s", (otp, mobile))
                logger.info(f"Updated OTP for existing user: {mobile}")
            else:
                cursor.execute(
                    "INSERT INTO users (mobile, otp) VALUES (%s, %s) RETURNING id", 
                    (mobile, otp)
                )
                logger.info(f"Created new user: {mobile}")
            
            session_id = f"session_{int(time.time())}_{random.randint(1000, 9999)}"
            user_sessions[session_id] = {"mobile": mobile, "verified": False}
            
            response = JSONResponse({"message": f"OTP sent to {mobile}"})
            response.set_cookie(key="session_id", value=session_id)
            return response
    except Exception as e:
        error_msg = f"Database error in login_or_register: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.post("/verify-otp")
async def verify_otp(mobile: str = Form(...), otp: str = Form(...), request: Request = None):
    logger.info(f"Verifying OTP for mobile: {mobile}")
    try:
           with get_db() as cursor:
            cursor.execute(
                "UPDATE users SET is_verified = TRUE WHERE mobile = %s AND otp = %s RETURNING mobile",
                (mobile, otp)
            )
            result = cursor.fetchone()
            
            if result:
                session_id = request.cookies.get("session_id")
                if session_id in user_sessions:
                    user_sessions[session_id]["verified"] = True
                    logger.info(f"OTP verified successfully for {mobile}")
                
                return JSONResponse({"message": "OTP verified successfully"})
            else:
                logger.warning(f"Invalid OTP attempt for {mobile}")
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid OTP"}
                )
    except Exception as e:
        error_msg = f"Database error in verify_otp: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.get("/search-users")
async def search_users(query: str = Query(..., min_length=3)):
    logger.info(f"Searching users with query: {query}")
    try:
           with get_db() as cursor:
            cursor.execute(
                "SELECT mobile FROM users WHERE mobile LIKE %s AND is_verified = TRUE",
                (f"%{query}%",)
            )
            users = [{"mobile": row[0]} for row in cursor.fetchall()]
            return {"users": users}
    except Exception as e:
        error_msg = f"Database error in search_users: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/", status_code=303)
    
    try:
        with open("static/search.html", "r") as f:
            return HTMLResponse(content=f.read())
    except Exception as e:
        logger.error(f"Failed to read search page: {str(e)}")
        return HTMLResponse(content="<html><body>Error loading search page. Please check server logs.</body></html>")

@app.get("/get-user-chats")
async def get_user_chats(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    logger.info(f"Getting chats for user: {current_user}")
    try:
           with get_db() as cursor:
            cursor.execute("""
                SELECT 
                    CASE WHEN sender = %s THEN receiver ELSE sender END AS partner,
                    MAX(timestamp) AS last_timestamp
                FROM messages
                WHERE sender = %s OR receiver = %s
                GROUP BY partner
                ORDER BY last_timestamp DESC
            """, (current_user, current_user, current_user))
            
            chats = []
            for partner, last_timestamp in cursor.fetchall():
                chat_id = get_chat_id(current_user, partner)
                
                cursor.execute("""
                    SELECT sender, message_text, timestamp 
                    FROM messages 
                    WHERE chat_id = %s 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """, (chat_id,))
                last_message = cursor.fetchone()
                
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM messages 
                    WHERE chat_id = %s 
                    AND sender != %s 
                    AND timestamp > COALESCE(
                        (SELECT last_read FROM user_chat_status 
                         WHERE user_id = %s AND chat_id = %s), 0
                    )
                """, (chat_id, current_user, current_user, chat_id))
                unread_count = cursor.fetchone()[0] or 0
                
                chats.append({
                    "user1": current_user,
                    "user2": partner,
                    "last_message": {
                        "sender": last_message[0],
                        "text": last_message[1],
                        "timestamp": last_message[2]
                    } if last_message else None,
                    "unread_count": unread_count,
                    "last_timestamp": last_timestamp  
                })
            
            return {"chats": chats}
    except Exception as e:
        error_msg = f"Database error in get_user_chats: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.get("/get-current-user")
async def get_user_endpoint(request: Request):
    current_user = get_current_user(request)
    if current_user:
        return {"user": current_user}
    return {"user": "Not logged in"}

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, to: str):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/", status_code=303)
    
    try:
        with open("static/chat.html", "r") as f:
            content = f.read().replace("{to}", to).replace("{current_user}", current_user)
            return HTMLResponse(content=content)
    except Exception as e:
        logger.error(f"Failed to read chat page: {str(e)}")
        return HTMLResponse(content="<html><body>Error loading chat page. Please check server logs.</body></html>")

@app.post("/send-message")
async def send_message(request: Request, to: str = Form(...), message: str = Form(...)):
    current_user = get_current_user(request)
    if not current_user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"}
        )
    
    logger.info(f"Sending message from {current_user} to {to}")
    try:
           with get_db() as cursor:
            chat_id = get_chat_id(current_user, to)
            timestamp = int(time.time())
            
            cursor.execute(
                """
                INSERT INTO messages (chat_id, sender, receiver, message_text, timestamp) 
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (chat_id, current_user, to, message, timestamp)
            )
            
            await sio.emit('new_message', {
                'chat_id': chat_id,
                'sender': current_user,
                'receiver': to,
                'message': message,
                'timestamp': timestamp
            }, room=chat_id)

            return {"status": "success"}
    except Exception as e:
        error_msg = f"Database error in send_message: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.post("/mark-messages-read")
async def mark_messages_read(request: Request, to: str):
    current_user = get_current_user(request)
    if not current_user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"}
        )
    
    logger.info(f"Marking messages read for {current_user} in chat with {to}")
    try:
        with closing(get_db()) as cursor:
            chat_id = get_chat_id(current_user, to)
            timestamp = int(time.time())
            
            cursor.execute("""
                INSERT INTO user_chat_status (user_id, chat_id, last_read)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, chat_id) 
                DO UPDATE SET last_read = EXCLUDED.last_read
            """, (current_user, chat_id, timestamp))
            
            return {"status": "success"}
    except Exception as e:
        error_msg = f"Database error in mark_messages_read: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.get("/get-messages")
async def get_messages(request: Request, to: str):
    current_user = get_current_user(request)
    if not current_user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"}
        )
    
    logger.info(f"Getting messages for {current_user} in chat with {to}")
    try:
           with get_db() as cursor:
            chat_id = get_chat_id(current_user, to)
            
            cursor.execute(
                """
                SELECT sender, message_text, timestamp 
                FROM messages 
                WHERE chat_id = %s 
                ORDER BY timestamp DESC
                """,
                (chat_id,)
            )
            
            messages = [{
                "text": row[1],
                "sender": row[0],
                "timestamp": row[2]
            } for row in cursor.fetchall()]
            
            return {"messages": messages}
    except Exception as e:
        error_msg = f"Database error in get_messages: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

@app.get("/check-new-messages")
async def check_new_messages(request: Request, to: str, after: str = ""):
    current_user = get_current_user(request)
    if not current_user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"}
        )
    
    logger.info(f"Checking new messages for {current_user} in chat with {to}")
    try:
           with get_db() as cursor:
            chat_id = get_chat_id(current_user, to)
            
            query = """
                SELECT sender, message_text, timestamp 
                FROM messages 
                WHERE chat_id = %s
            """
            params = [chat_id]
            
            if after and after.isdigit():
                query += " AND timestamp > %s"
                params.append(int(after))
                
            query += " ORDER BY timestamp"
            
            cursor.execute(query, params)
            
            messages = [{
                "text": row[1],
                "sender": row[0],
                "timestamp": row[2]
            } for row in cursor.fetchall()]
            
            return {"messages": messages}
    except Exception as e:
        error_msg = f"Database error in check_new_messages: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": error_msg}
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)