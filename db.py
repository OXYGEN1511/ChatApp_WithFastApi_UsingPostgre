import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

class PostgresDB:
    _connection_pool = None
    
    @classmethod
    def initialize(cls, host="dpg-d0eou8k9c44c7389k4hg-a", user="chatuser", password="vJ0E8nmutpbQvyapOL7tL3QdButMCJga", database="chatdb_u6dg", port=5432):
        """Initialize the database connection pool"""
        try:
            cls._connection_pool = pool.SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                host=host,
                user=user,
                password=password,
                database=database,
                port=port
            )
            print("Database connection pool initialized successfully")
        except Exception as e:
            print(f"Error initializing database pool: {str(e)}")
            raise
    
    @classmethod
    @contextmanager
    def get_connection(cls):
        """Get a connection from the pool"""
        if cls._connection_pool is None:
            cls.initialize()
        
        conn = cls._connection_pool.getconn()
        try:
            yield conn
        finally:
            cls._connection_pool.putconn(conn)
    
    @classmethod
    @contextmanager
    def get_cursor(cls):
        """Get a cursor from the connection pool"""
        with cls.get_connection() as conn:
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                raise e
            finally:
                cursor.close()


try:
    PostgresDB.initialize()
except Exception as e:
    print(f"Failed to initialize database: {str(e)}")

def get_db():
    """Function to get a database cursor"""
    return PostgresDB.get_cursor()

# Example of correct usage in your FastAPI endpoint:
# @app.post("/login-or-register")
# async def login_or_register(mobile: str = Form(...)):
#     try:
#         with get_db() as cursor:  # Use context manager directly
#             # Use cursor here
#     except Exception as e:
#         # Handle exception