from fastapi import FastAPI, Form, Request, Query, Depends, HTTPException, Response, Header
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi import status
import random
import time
import logging
import socketio
import traceback
import jwt
from typing import Optional, Dict, List
from db import get_db, init_db
import uvicorn
from datetime import datetime, timedelta
import secrets
import string



secret_key = ''.join(secrets.choice(string.ascii_letters + string.digits + '!@#$%^&*(-_=+)') for _ in range(64))
print(secret_key)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


app = FastAPI()


JWT_SECRET = secret_key  
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = 60 * 24 


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail)}
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc)}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {str(exc)}")
    logger.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )





sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio)
app.mount('/socket.io', socket_app)



connected_users = {}    
sid_to_user = {}        
user_rooms = {}         


def create_access_token(data: dict):
    """Create a JWT access token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRATION_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    print(encoded_jwt)
    return encoded_jwt


def get_current_user(authorization: Optional[str] = Header(None)):
    """Get the current user from the Authorization header"""
    if not authorization:
        return None
    
    try:
        token = authorization.split(" ")[1] if "Bearer" in authorization else authorization
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        mobile = payload.get("sub")
        if mobile is None:
            return None
        return mobile
    except jwt.PyJWTError:
        return None


def get_chat_id(user1: str, user2: str):
    print('////////////', user1, user2)
    users = sorted([user1, user2])
    return f"{users[0]}:{users[1]}"


async def get_user_chat_list(mobile: str):
    """Get the list of chats for a user - used by socket.io and API"""
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
            """, (mobile, mobile, mobile,))
            
            chats = []
            for partner_row in cursor.fetchall():
                partner = partner_row[0]    
                chat_id = get_chat_id(mobile, partner)
                
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
                """, (chat_id, mobile, mobile, chat_id,))
                unread_count = cursor.fetchone()[0] or 0
                
                chats.append({
                    "user1": mobile,
                    "user2": partner,
                    "chat_id": chat_id,
                    "last_message": {
                        "sender": last_message[0],
                        "text": last_message[1],
                        "timestamp": last_message[2]
                    } if last_message else None,
                    "unread_count": unread_count
                })
            
            return chats
    except Exception as e:
        logger.error(f"Error getting user chat list: {str(e)}")
        return []


async def get_chat_messages(chat_id: str, after_timestamp: int = 0):
    """Get messages for a chat - used by socket.io and API"""
    try:
        with get_db() as cursor:
            query = """ 
                SELECT sender, message_text, timestamp 
                FROM messages 
                WHERE chat_id = %s
            """
            params = [chat_id]
            
            if after_timestamp > 0:
                query += " AND timestamp > %s"
                params.append(after_timestamp)
                
            query += " ORDER BY timestamp"
            
            cursor.execute(query, params)
            
            messages = [{
                "sender": row[0],
                "text": row[1],
                "timestamp": row[2]
            } for row in cursor.fetchall()]
            
            return messages
    except Exception as e:
        logger.error(f"Error getting chat messages: {str(e)}")
        return []


async def mark_messages_read_internal(user: str, chat_id: str):
    """Mark messages as read - used by socket.io and API"""
    try:
        with get_db() as cursor:
            timestamp = int(time.time())
            
            
            cursor.execute("""
                INSERT INTO user_chat_status (user_id, chat_id, last_read)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, chat_id) 
                DO UPDATE SET last_read = EXCLUDED.last_read
            """, (user, chat_id, timestamp))
            
            
            users = chat_id.split(':')
            other_user = users[0] if users[0] != user else users[1]
            
            if user in connected_users:
                await sio.emit('messages_read_self',  {
                    'chat_id': chat_id,
                    'reader': user,
                    'timestamp': timestamp
                }, 
                room=connected_users[user])

            if other_user in connected_users:
                await sio.emit('messages_read', {
                    'chat_id': chat_id,
                    'reader': user,
                    'timestamp': timestamp
                }, room=connected_users[other_user])
            
            return timestamp
    except Exception as e:
        logger.error(f"Error marking messages read: {str(e)}")
        return int(time.time())




@sio.event
async def connect(sid, environ, auth=None):
    logger.info(f"Client connected: {sid}")
    sid_to_user[sid] = None  
    return True


@sio.event
async def authenticate(sid, data):
    if 'token' in data:
        try:
            token = data['token']
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            mobile = payload.get("sub")
            if mobile:
                
                if mobile in connected_users:
                    old_sid = connected_users[mobile]
                    if old_sid in sid_to_user:
                        del sid_to_user[old_sid]
                
                connected_users[mobile] = sid
                sid_to_user[sid] = mobile
                user_rooms[mobile] = []
                
                logger.info(f"User {mobile} authenticated socket connection")
                
                
                chats = await get_user_chat_list(mobile)
                
                
                for chat in chats:
                    chat_id = chat['chat_id']
                    await sio.enter_room(sid, chat_id)
                    user_rooms[mobile].append(chat_id)
                
                
                for chat in chats:
                    other_user = chat['user2']
                    if other_user in connected_users:
                        await sio.emit('user_status', {
                            'user': mobile,
                            'status': 'online'
                        }, room=connected_users[other_user])
                
                
                await sio.emit('chat_list', {'chats': chats}, room=sid)
                
                return {"status": "authenticated", "user": mobile}
        except jwt.PyJWTError as e:
            logger.error(f"JWT error: {str(e)}")
    
    return {"status": "authentication_failed"}


@sio.event
async def join_chat(sid, data):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    if 'chat_id' in data:
        chat_id = data['chat_id']
        
    
        users = chat_id.split(':')
        if user not in users:
            return {"status": "unauthorized"}
        
        
        await sio.enter_room(sid, chat_id)
        if user in user_rooms and chat_id not in user_rooms[user]:
            user_rooms[user].append(chat_id)
        
        logger.info(f"User {user} joined chat room: {chat_id}")
        

        messages = await get_chat_messages(chat_id)
        
        
        await mark_messages_read_internal(user, chat_id)
        
        return {
            "status": "joined",
            "chat_id": chat_id,
            "messages": messages
        }

    return {"status": "chat_id_missing"}



@sio.event
async def leave_chat(sid, data):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    if 'chat_id' in data:
        chat_id = data['chat_id']
        await sio.leave_room(sid, chat_id)
        
        if user in user_rooms and chat_id in user_rooms[user]:
            user_rooms[user].remove(chat_id)
        
        logger.info(f"User {user} left chat room: {chat_id}")
        return {"status": "success"}
    
    return {"status": "invalid_request"}


@sio.event
async def send_message(sid, data):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    try:
        receiver = data.get('to')
        message_text = data.get('message')
        
        if not receiver or not message_text:
            return {"status": "invalid_request"}
        
        logger.info(f"Socket.IO: Sending message from {user} to {receiver}")
        
        with get_db() as cursor:
            chat_id = get_chat_id(user, receiver)
            timestamp = int(time.time())
            
            cursor.execute(
                """
                INSERT INTO messages (chat_id, sender, receiver, message_text, timestamp) 
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (chat_id, user, receiver, message_text, timestamp)
            )
            message_id = cursor.fetchone()[0]
            
            
            if user in user_rooms and chat_id not in user_rooms[user]:
                await sio.enter_room(sid, chat_id)
                user_rooms[user].append(chat_id)
            
            
            message_data = {
                'id': message_id,
                'chat_id': chat_id,
                'sender': user,
                'receiver': receiver,
                'text': message_text,
                'timestamp': timestamp
            }
            
            
            await sio.emit('new_message', message_data, room=chat_id)
            
            
            for participant in [user, receiver]:
                if participant in connected_users:
                    chats = await get_user_chat_list(participant)
                    await sio.emit('chat_list_update', {'chats': chats}, room=connected_users[participant])
            
            return {
                "status": "success", 
                "message_id": message_id,
                "timestamp": timestamp
            }
            
    except Exception as e:
        error_msg = f"Error in send_message socket event: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}


@sio.event
async def mark_read(sid, data):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    try:
        chat_id = data.get('chat_id')
        
        if not chat_id:
            return {"status": "invalid_request"}
        
    
        users = chat_id.split(':')
        if user not in users:
            return {"status": "unauthorized"}
        
        timestamp = await mark_messages_read_internal(user, chat_id)
        
        return {"status": "success", "timestamp": timestamp}
    except Exception as e:
        error_msg = f"Error in mark_read socket event: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}


@sio.event
async def get_chat_history(sid, data):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    try:
        chat_id = data.get('chat_id')
        after = data.get('after', 0)
        
        if not chat_id:
            return {"status": "invalid_request"}
        
        
        users = chat_id.split(':')
        if user not in users:
            return {"status": "unauthorized"}
        
        messages = await get_chat_messages(chat_id, after)
        
        return {"status": "success", "messages": messages}
    except Exception as e:
        error_msg = f"Error in get_chat_history socket event: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}


@sio.event
async def refresh_chat_list(sid, data=None):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    try:
        chats = await get_user_chat_list(user)
        return {"status": "success", "chats": chats}
    except Exception as e:
        error_msg = f"Error in refresh_chat_list socket event: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}


@sio.event
async def typing(sid, data):
    if sid not in sid_to_user:
        return {"status": "not_authenticated"}
    
    user = sid_to_user[sid]
    if not user:
        return {"status": "not_authenticated"}
    
    try:
        chat_id = data.get('chat_id')
        is_typing = data.get('typing', True)
        
        if not chat_id:
            return {"status": "invalid_request"}
        
        
        users = chat_id.split(':')
        if user not in users:
            return {"status": "unauthorized"}
        
        
        await sio.emit('user_typing', {
            'chat_id': chat_id,
            'user': user,
            'typing': is_typing
        }, room=chat_id, skip_sid=sid)  
        
        return {"status": "success"}
    except Exception as e:
        error_msg = f"Error in typing socket event: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}


@sio.event
async def disconnect(sid):
    user = sid_to_user.get(sid)
    if user:
        logger.info(f"User {user} disconnected")
        
        
        if user in connected_users and connected_users[user] == sid:
            del connected_users[user]
        
        
        if user in user_rooms:
            for chat_id in user_rooms[user]:
                users = chat_id.split(':')
                other_user = users[0] if users[0] != user else users[1]
                
                if other_user in connected_users:
                    await sio.emit('user_status', {
                        'user': user,
                        'status': 'offline'
                    }, room=connected_users[other_user])
            
            del user_rooms[user]
    
    if sid in sid_to_user:
        del sid_to_user[sid]
    
    logger.info(f"Client disconnected: {sid}")


@app.on_event("startup")
async def startup_db_client():
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")

# ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.post("/login-or-register")
async def login_or_register(
    mobile: str = Form(...)
): 
    
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
            
           
            return JSONResponse({
                "message": f"OTP sent to {mobile}",
                "otp": otp, 
            })
            
    except Exception as e:
        error_msg = f"Database error in login_or_register: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )


# ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.post("/verify-otp")
async def verify_otp(
    mobile: str = Form(...),
    otp: str = Form(...)
):
    logger.info(f"Verifying OTP for mobile: {mobile}")
    
    try:
    
        with get_db() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE mobile = %s AND otp = %s",
                (mobile, otp)
            )
            user = cursor.fetchone()
            
            if user:
                access_token=create_access_token(data={"sub":mobile})
                
                cursor.execute(
                    "UPDATE users SET is_verified = TRUE WHERE mobile = %s",
                    (mobile,)
                )
                
                
                # access_token = create_access_token({"sub": mobile})
                
                return JSONResponse({
                    "message": "OTP verified successfully",
                    "verified": True,
                    "mobile": mobile,
                    "access_token": access_token,
                    "token_type": "bearer"
                })
            else:
                logger.warning(f"Invalid OTP attempt for {mobile}")
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid OTP"}
                )
            
    except Exception as e:
        error_msg = f"Database error in verify_otp: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )


# ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.get("/get-current-user")
async def get_current_user_endpoint(authorization: Optional[str] = Header(None)):
    try:
        current_user = get_current_user(authorization)
        if current_user:
            return JSONResponse({
                "user": current_user,
                "online": current_user in connected_users
            })
        return JSONResponse(
            status_code=401,
            content={"user": None, "detail": "Not authenticated"}
        )
    except Exception as e:
        logger.error(f"Error in get_current_user_endpoint: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": str(e)}
        )

# ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.get("/search-users")
async def search_users(
    query: str = Query(..., min_length=1),
    authorization: Optional[str] = Header(None)
):
    try:
        logger.info(f"Authorization header received: {authorization}")
        
        current_user = get_current_user(authorization)
        if not current_user:
            logger.warning("Unauthorized access attempt")
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        
        logger.info(f"User {current_user} searching with query: {query}")
        
        with get_db() as cursor:
            search_query = f"%{query}%"
            logger.info(f"Executing search with pattern: {search_query}")
            
            cursor.execute(
                """SELECT mobile FROM users 
                WHERE mobile LIKE %s 
                AND mobile != %s
                AND is_verified = TRUE
                ORDER BY mobile DESC
                LIMIT 20""",
                (search_query, current_user)
            )
            
            users = cursor.fetchall()
            logger.info(f"Found {len(users)} matching users")
            
            result = []
            for row in users:
                mobile = row[0]
                is_online = mobile in connected_users
                result.append({
                    "mobile": mobile,
                    "online": is_online
                })
            
            return JSONResponse({
                "status": "success",
                "count": len(result),
                "users": result
            })
                
    except Exception as e:
        logger.error(f"Search error: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error during search",
                "error": str(e)
            }
        )

# ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????? 
@app.get("/get-user-chats")
async def get_user_chats(authorization: Optional[str] = Header(None)):
    try:
        current_user = get_current_user(authorization)
        if not current_user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )

        logger.info(f"Getting chats for user: {current_user}")
        
        chats = await get_user_chat_list(current_user)
        return JSONResponse({"chats": chats})
    except Exception as e:
        error_msg = f"Database error in get_user_chats: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )


# ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.post("/send-message")
async def send_message(
    to: str = Form(...), 
    message: str = Form(...),
    authorization: Optional[str] = Header(None)
):
    try:
        current_user = get_current_user(authorization)
        if not current_user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        
        logger.info(f"API: Sending message from {current_user} to {to}")
        
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
            message_id = cursor.fetchone()[0]
            
            # Prepare message data
            message_data = {
                'id': message_id,
                'chat_id': chat_id,
                'sender': current_user,
                'receiver': to,
                'text': message,
                'timestamp': timestamp
            }
            
            # Emit event to socket.io room
            await sio.emit('new_message', message_data, room=chat_id)
            
            # Update chat lists
            for participant in [current_user, to]:
                if participant in connected_users:
                    chats = await get_user_chat_list(participant)
                    await sio.emit('chat_list_update', {'chats': chats}, room=connected_users[participant])

            return JSONResponse({
                "status": "success",
                "message_id": message_id,
                "timestamp": timestamp
            })
    except Exception as e:
        error_msg = f"Database error in send_message: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )

# ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????  
@app.get("/get-messages")
async def get_messages(
    to: str,
    authorization: Optional[str] = Header(None)
):
    try:
        current_user = get_current_user(authorization)
        if not current_user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        
        logger.info(f"Getting messages for {current_user} in chat with {to}")
        
        chat_id = get_chat_id(current_user, to)
        messages = await get_chat_messages(chat_id)
        
        # Mark as read too
        await mark_messages_read_internal(current_user, chat_id)
        
        return JSONResponse({"messages": messages})
    except Exception as e:
        error_msg = f"Database error in get_messages: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )
    

# class MaskMessageBody(BaseModel):
#     to: Optional[str] = None

# ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.post("/mark-messages-read")
async def mark_messages_read(
    to: str,
    authorization: Optional[str] = Header(None)
):
    try:
        
        current_user = get_current_user(authorization)
        if not current_user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        
        logger.info(f"Marking messages read for {current_user} in chat with {to}")
        
        chat_id = get_chat_id(current_user, to)
        timestamp = await mark_messages_read_internal(current_user, chat_id)
        
        return JSONResponse({
            "status": "success", 
            "timestamp": timestamp,
            "chat_id":chat_id
        })
    except Exception as e:
        error_msg = f"Database error in mark_messages_read: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )

# ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.get("/check-new-messages")
async def check_new_messages(
    to: str, 
    after: str = "",
    authorization: Optional[str] = Header(None)
):
    try:
        current_user = get_current_user(authorization)
        if not current_user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        
        logger.info(f"Checking new messages for {current_user} in chat with {to}")
        
        chat_id = get_chat_id(current_user, to)
        
        messages = await get_chat_messages(
            chat_id, 
            int(after) if after and after.isdigit() else 0
        )
        
        return JSONResponse({"messages": messages})
    except Exception as e:
        error_msg = f"Database error in check_new_messages: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )

# ?????????????????????????????????????????????????????????????????????????????????????????????????????????????
@app.post("/refresh-token")
async def refresh_token(authorization: Optional[str] = Header(None)):
    try:
        current_user = get_current_user(authorization)
        if not current_user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"}
            )
        
        
        access_token = create_access_token({"sub": current_user})
        
        return JSONResponse({
            "access_token": access_token,
            "token_type": "bearer"
        })
    except Exception as e:
        error_msg = f"Error refreshing token: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": error_msg}
        )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
