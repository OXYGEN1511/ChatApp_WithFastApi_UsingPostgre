#!/usr/bin/env python3
"""
Test script to verify database connection
Run this script to check if your database is properly configured
"""

import psycopg2
import sys

def test_connection(host="localhost", user="postgres", password="8866210765", database="chat_db", port=5432):
    """Test the database connection with the given parameters"""
    try:
        print(f"\nAttempting to connect to PostgreSQL database: {database}")
        print(f"Host: {host}, Port: {port}, User: {user}")
        
        # Try to establish a connection
        conn = psycopg2.connect(
            host=host,
            user=user,
            password=password,
            database=database,
            port=port
        )
        
        # Get the cursor
        cursor = conn.cursor()
        
        # Execute a simple query to verify connection
        cursor.execute("SELECT version();")
        db_version = cursor.fetchone()
        
        print("\n✅ CONNECTION SUCCESSFUL!")
        print(f"Database version: {db_version[0]}")
        
        # Check if required tables exist
        print("\nChecking if required tables exist:")
        tables = ['users', 'messages', 'user_chat_status']
        for table in tables:
            cursor.execute(f"SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = '{table}');")
            exists = cursor.fetchone()[0]
            if exists:
                print(f"  ✅ Table '{table}' exists")
            else:
                print(f"  ❌ Table '{table}' does not exist")
        
        # Close connection
        cursor.close()
        conn.close()
        print("\nConnection closed.")
        return True
        
    except Exception as e:
        print("\n❌ DATABASE CONNECTION FAILED!")
        print(f"Error: {e}")
        
        # Provide troubleshooting suggestions
        print("\nTroubleshooting suggestions:")
        print("1. Make sure PostgreSQL is running. Run 'pg_ctl status' to check.")
        print("2. Verify the database 'chat_db' exists. You can create it with 'CREATE DATABASE chat_db;'")
        print("3. Check username and password.")
        print("4. Ensure PostgreSQL is listening on the correct port (default is 5432).")
        print("5. Check if the PostgreSQL service is started.")
        
        return False

if __name__ == "__main__":
    # Check if custom connection parameters are provided
    if len(sys.argv) > 1:
        # If parameters are provided, use them
        host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
        user = sys.argv[2] if len(sys.argv) > 2 else "postgres"
        password = sys.argv[3] if len(sys.argv) > 3 else "8866210765"
        database = sys.argv[4] if len(sys.argv) > 4 else "chat_db"
        port = int(sys.argv[5]) if len(sys.argv) > 5 else 5432
        
        test_connection(host, user, password, database, port)
    else:
        # Use default parameters
        test_connection()