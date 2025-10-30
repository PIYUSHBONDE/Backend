# jira_service.py - Place alongside main.py

import os
import requests
from datetime import datetime, timedelta
from typing import Optional
from models import SessionLocal, JiraConnection
from fastapi import HTTPException

# OAuth Config
JIRA_OAUTH_CLIENT_ID = os.getenv("JIRA_OAUTH_CLIENT_ID")
JIRA_OAUTH_CLIENT_SECRET = os.getenv("JIRA_OAUTH_CLIENT_SECRET")
JIRA_OAUTH_CALLBACK_URL = os.getenv("JIRA_OAUTH_CALLBACK_URL")


def refresh_token_if_needed(connection: JiraConnection) -> bool:
    """Auto-refresh token if expired."""
    if not connection.refresh_token:
        return False
    
    try:
        token_url = "https://auth.atlassian.com/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": JIRA_OAUTH_CLIENT_ID,
            "client_secret": JIRA_OAUTH_CLIENT_SECRET,
            "refresh_token": connection.refresh_token
        }
        
        response = requests.post(token_url, json=payload)
        response.raise_for_status()
        tokens = response.json()
        
        db = SessionLocal()
        try:
            connection.access_token = tokens["access_token"]
            if tokens.get("refresh_token"):
                connection.refresh_token = tokens["refresh_token"]
            connection.token_expires_at = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))
            db.commit()
            return True
        finally:
            db.close()
    except:
        return False


def get_valid_connection(user_id: str) -> Optional[JiraConnection]:
    """Get connection with auto-refresh."""
    db = SessionLocal()
    try:
        conn = db.query(JiraConnection).filter(
            JiraConnection.user_id == user_id,
            JiraConnection.is_active == True
        ).first()
        
        if not conn:
            return None
        
        # Refresh if expiring in 5 minutes
        if conn.token_expires_at < datetime.utcnow() + timedelta(minutes=5):
            if not refresh_token_if_needed(conn):
                conn.is_active = False
                db.commit()
                return None
        
        return conn
    finally:
        db.close()


def fetch_jira_projects(user_id: str):
    """Get user's Jira projects via Atlassian API (OAuth 2.0 3LO)."""
    conn = get_valid_connection(user_id)
    if not conn:
        raise HTTPException(400, "Jira not connected. Please connect your Jira account first.")
    
    try:
        # ✅ Correct API endpoint for OAuth 2.0
        url = f"https://api.atlassian.com/ex/jira/{conn.jira_cloud_id}/rest/api/3/project"
        headers = {"Authorization": f"Bearer {conn.access_token}"}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 401:
            raise HTTPException(401, "Unauthorized. Jira token may have expired.")
        
        response.raise_for_status()
        projects = response.json()
        
        return {
            "projects": [{"key": p["key"], "name": p["name"]} for p in projects]
        }
    except Exception as e:
        print(f"❌ Fetch projects error: {e}")
        return {"error": f"Failed to fetch projects: {str(e)}"}


def fetch_jira_requirements(user_id: str, project_key: str):
    """Fetch requirements (stories / labeled items) from Jira."""
    conn = get_valid_connection(user_id)
    if not conn:
        raise HTTPException(400, "Jira not connected.")
    
    try:
        # ✅ Correct API endpoint for OAuth 2.0
        url = f"https://api.atlassian.com/ex/jira/{conn.jira_cloud_id}/rest/api/3/search/jql"
        headers = {"Authorization": f"Bearer {conn.access_token}"}
        
        jql = f'project = {project_key} AND (issuetype = "Story" OR labels = "Requirement")'
        params = {
            "jql": jql,
            "maxResults": 100,
            "fields": "summary,description,priority,labels,status"
        }
        
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 401:
            raise HTTPException(401, "Unauthorized. Jira token may have expired.")
        if response.status_code >= 400:
            print("🚨 Jira API error:", response.status_code, response.text)
        
        response.raise_for_status()
        data = response.json()
        
        requirements = []
        for idx, issue in enumerate(data.get("issues", []), 1):
            fields = issue["fields"]
            priority = fields.get("priority", {}).get("name", "Medium")
            risk = {"High": "high", "Medium": "medium", "Low": "low"}.get(priority, "medium")
            
            requirements.append({
                "id": f"REQ-{idx:03d}",
                "jira_key": issue["key"],
                # Use base URL for browser link, not API domain
                "jira_url": f"{conn.jira_base_url}/browse/{issue['key']}",
                "text": fields.get("summary", ""),
                "risk_level": risk,
                "compliance_standard": "None",
                "type": "functional"
            })
        
        return {"status": "success", "requirements": requirements}
    except Exception as e:
        print(f"❌ Fetch requirements error: {e}")
        return {"error": f"Failed to fetch requirements: {str(e)}"}


def create_jira_test_case(user_id: str, project_key: str, test_case: dict, requirement_key: str = None):
    """Create a Jira issue for a test case."""
    conn = get_valid_connection(user_id)
    if not conn:
        raise HTTPException(400, "Jira not connected.")
    
    try:
        # ✅ Correct API endpoint for OAuth 2.0
        url = f"https://api.atlassian.com/ex/jira/{conn.jira_cloud_id}/rest/api/3/issue"
        headers = {
            "Authorization": f"Bearer {conn.access_token}",
            "Content-Type": "application/json"
        }

        # Build description using Atlassian document format
        steps = "\n".join([f"{i}. {s}" for i, s in enumerate(test_case.get("steps", []), 1)])
        description_text = f"""Test Case: {test_case.get('id', '')}

Steps:
{steps}

Expected Result:
{test_case.get('expected', '')}
"""

        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": test_case.get("title"),
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description_text}]}
                    ],
                },
                "issuetype": {"name": "Task"},
                "labels": ["healthcase-ai", "automated-test"]
            }
        }
        
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 401:
            raise HTTPException(401, "Unauthorized. Jira token may have expired.")
        
        response.raise_for_status()
        created = response.json()
        
        return {
            "status": "success",
            "jira_key": created["key"],
            "jira_url": f"{conn.jira_base_url}/browse/{created['key']}"
        }
    except Exception as e:
        print(f"❌ Create test case error: {e}")
        return {"error": f"Failed to create test case: {str(e)}"}