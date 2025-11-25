import os
import json
import logging
import urllib.parse
import urllib3
from botocore.session import Session
from botocore.awsrequest import AWSRequest
from botocore.auth import SigV4Auth
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
REGION = os.environ.get("AWS_REGION", "us-east-1")
OS_ENDPOINT = os.environ.get("OS_ENDPOINT", "").rstrip("/")
OS_INDEX = os.environ.get("OS_INDEX", "photos")
LEX_BOT_ID = os.environ.get("LEX_BOT_ID", "")
LEX_BOT_ALIAS_ID = os.environ.get("LEX_BOT_ALIAS_ID", "")
LEX_LOCALE_ID = os.environ.get("LEX_LOCALE_ID", "en_US")

# Validate required environment variables
if not OS_ENDPOINT:
    raise ValueError("OS_ENDPOINT environment variable is required")
if not LEX_BOT_ID:
    raise ValueError("LEX_BOT_ID environment variable is required")
if not LEX_BOT_ALIAS_ID:
    raise ValueError("LEX_BOT_ALIAS_ID environment variable is required")

# AWS clients
lex = boto3.client("lexv2-runtime", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION) 
http = urllib3.PoolManager()
credentials = Session().get_credentials().get_frozen_credentials()


def os_signed_request(method, path, body=None, params=None):
    """Send a SigV4-signed HTTP request to OpenSearch."""
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{OS_ENDPOINT}{path}?{qs}"
    else:
        url = f"{OS_ENDPOINT}{path}"

    headers = {
        "Host": urllib.parse.urlparse(OS_ENDPOINT).netloc,
        "Content-Type": "application/json"
    }

    data = body if isinstance(body, (str, bytes)) else (json.dumps(body) if body else None)

    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(credentials, "es", REGION).add_auth(req)

    resp = http.request(method, url, body=data, headers=dict(req.headers))
    if resp.status not in (200, 201):
        raise RuntimeError(f"OpenSearch {resp.status}: {resp.data.decode('utf-8','ignore')}")
    return json.loads(resp.data)


def query_lex(query_text, session_id="default-session"):
    """Send user query to Lex V2 and extract slot values."""
    try:
        resp = lex.recognize_text(
            botId=LEX_BOT_ID,
            botAliasId=LEX_BOT_ALIAS_ID,
            localeId=LEX_LOCALE_ID,
            sessionId=session_id,
            text=query_text
        )
        
        logger.info("Lex response: %s", json.dumps(resp))
        
        # Extract slots that contain keywords
        interpretations = resp.get("interpretations", [])
        if not interpretations:
            logger.warning("No interpretations from Lex")
            return []
        
        intent = interpretations[0].get("intent", {})
        slots = intent.get("slots", {})
        
        keywords = []
        for slot_name, slot_data in slots.items():
            if not slot_data:
                continue
                
            # Try different ways to extract the value
            value = None
            if isinstance(slot_data, dict):
                # Check for interpretedValue (Lex V2 format)
                if "value" in slot_data and isinstance(slot_data["value"], dict):
                    value = slot_data["value"].get("interpretedValue")
                # Fallback to direct value
                if not value:
                    value = slot_data.get("value")
                # Fallback to originalValue
                if not value:
                    value = slot_data.get("originalValue")
            
            if value:
                # Handle both string and dict values
                if isinstance(value, dict):
                    value = value.get("interpretedValue") or value.get("originalValue")
                if value:
                    keywords.append(str(value))
        
        keywords = [k.lower().strip() for k in keywords if k]
        logger.info("Extracted keywords: %s", keywords)
        return keywords
        
    except Exception as e:
        logger.error("Lex query failed: %s", e, exc_info=True)
        return []


def search_photos_in_os(keywords, size=20):
    """Search OpenSearch for photos matching keywords in 'labels' field."""
    if not keywords:
        return []

    query = {
        "size": size,
        "query": {
            "bool": {
                "should": [{"match": {"labels": kw}} for kw in keywords],
                "minimum_should_match": 1
            }
        }
    }
    
    try:
        res = os_signed_request("POST", f"/{OS_INDEX}/_search", body=query)
        hits = res.get("hits", {}).get("hits", [])
        results = []
        for h in hits:
            source = h["_source"]
            bucket = source["bucket"]
            object_key = source["objectKey"]
            labels = source.get("labels", [])  # Extract labels
            
            # Generate presigned URL (or use public URL if bucket is public)
            photo_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': object_key},
                ExpiresIn=3600
            )
            
            results.append({
                "url": photo_url,
                "labels": labels
            })
        
        logger.info("OpenSearch returned %d hits", len(results))
        return results
    except Exception as e:
        logger.error("OpenSearch search failed: %s", e, exc_info=True)
        return []

def lambda_handler(event, context):
    """Lambda handler for /search?q=<query> requests."""
    try:
        logger.info("Received event: %s", json.dumps(event))

        # Extract query string from API Gateway
        query_params = event.get("queryStringParameters") or {}
        query_text = query_params.get("q", "").strip()
        
        if not query_text:
            logger.info("Empty query, returning empty results")
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"  # Add CORS if needed
                },
                "body": json.dumps([])
            }

        # Disambiguate query using Lex
        keywords = query_lex(query_text)
        logger.info("Query '%s' disambiguated to keywords: %s", query_text, keywords)

        # If no keywords found, return empty results
        if not keywords:
            logger.info("No keywords extracted, returning empty results")
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps([])
            }

        # Search OpenSearch
        results = search_photos_in_os(keywords)
        logger.info("Found %d results for keywords: %s", len(results), keywords)

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({"results": results})  # Wrap in "results" object
        }
        
    except Exception as e:
        logger.error("Error in lambda_handler: %s", e, exc_info=True)
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({"results": []})
        }