import os
import json
import logging
import urllib.parse
import urllib3
from datetime import datetime, timezone
from botocore.session import Session
from botocore.awsrequest import AWSRequest
from botocore.auth import SigV4Auth
import boto3

#SUCCESSS IF THIS COMMENT IS HERE 3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables with defaults
REKOGNITION_MIN_CONF = float(os.environ.get("REKOGNITION_MIN_CONF", "70.0"))  # percent
OS_ENDPOINT = os.environ.get("OS_ENDPOINT", "").rstrip("/")  # e.g. https://search-photos-abc123.us-east-1.es.amazonaws.com
OS_INDEX = os.environ.get("OS_INDEX", "photos")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Validate required environment variables
if not OS_ENDPOINT:
    raise ValueError("OS_ENDPOINT environment variable is required")

# Initialize AWS clients
s3 = boto3.client("s3")
rek = boto3.client("rekognition")

# Initialize HTTP client and credentials for OpenSearch
http = urllib3.PoolManager()
credentials = Session().get_credentials().get_frozen_credentials()


def os_signed_request(method, path, body=None, params=None):
    """Send a SigV4-signed HTTP request to OpenSearch."""
    try:
        if params:
            qs = urllib.parse.urlencode(params)
            url = f"{OS_ENDPOINT}{path}?{qs}"
        else:
            url = f"{OS_ENDPOINT}{path}"

        logger.info("OpenSearch request: %s %s", method, url)

        headers = {
            "Host": urllib.parse.urlparse(OS_ENDPOINT).netloc,
            "Content-Type": "application/json"
        }

        data = body if isinstance(body, (str, bytes)) else (json.dumps(body) if body else None)
        logger.info("Request body: %s", data[:500] if data and len(str(data)) > 500 else data)

        req = AWSRequest(method=method, url=url, data=data, headers=headers)
        SigV4Auth(credentials, "es", REGION).add_auth(req)

        logger.info("Sending request to OpenSearch...")
        resp = http.request(method, url, body=data, headers=dict(req.headers))
        logger.info("OpenSearch response status: %s", resp.status)
        
        if resp.status not in (200, 201):
            error_msg = resp.data.decode('utf-8', 'ignore')
            logger.error("OpenSearch error response: %s", error_msg)
            raise RuntimeError(f"OpenSearch {resp.status}: {error_msg}")
        
        result = json.loads(resp.data)
        logger.info("OpenSearch request successful")
        return result
    except Exception as e:
        logger.error("os_signed_request failed: %s", str(e), exc_info=True)
        raise


def get_custom_labels(bucket, key):
    """
    Retrieve custom labels from S3 object metadata.
    Returns a list of custom label strings.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        meta = head.get("Metadata", {})
        # S3 lower-cases metadata keys by default; AWS docs show custom headers as x-amz-meta-...
        raw = meta.get("customlabels") or meta.get("customlabels".lower())
        if not raw:
            return []
        # Split on commas and strip
        labels = [lbl.strip() for lbl in raw.split(",") if lbl.strip()]
        return labels
    except Exception as e:
        logger.warning("Failed to head_object for %s/%s: %s", bucket, key, e)
        return []


def get_rekognition_labels(bucket, key, min_conf=REKOGNITION_MIN_CONF):
    """
    Use Rekognition to detect labels in the image.
    Returns a list of label names (strings).
    """
    try:
        response = rek.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=50
        )
        labels = []
        for lab in response.get("Labels", []):
            if lab.get("Confidence", 0.0) >= min_conf:
                labels.append(lab["Name"])
        return labels
    except Exception as e:
        logger.error("Rekognition detect_labels failed for %s/%s: %s", bucket, key, e)
        return []


def normalize_labels(labels):
    """
    Normalize labels: lowercase, strip whitespace, and deduplicate.
    Preserves order of first occurrence.
    """
    normalized = []
    seen = set()
    for l in labels:
        nl = l.strip().lower()
        if nl and nl not in seen:
            seen.add(nl)
            normalized.append(nl)
    return normalized


def index_document(doc_id, body):
    """
    Index the document in ElasticSearch.
    """
    try:
        logger.info("Attempting to index document: %s", doc_id)
        logger.info("Document body: %s", json.dumps(body))
        logger.info("OpenSearch endpoint: %s", OS_ENDPOINT)
        logger.info("OpenSearch index: %s", OS_INDEX)
        
        # URL-encode the doc_id to handle special characters properly
        encoded_doc_id = urllib.parse.quote(doc_id, safe='')
        path = f"/{OS_INDEX}/_doc/{encoded_doc_id}"
        logger.info("Full path: %s", path)
        
        res = os_signed_request("PUT", path, body=body)
        logger.info("Document indexed successfully: %s", doc_id)
        logger.info("OpenSearch response: %s", json.dumps(res))
        return res
    except Exception as e:
        logger.error("Failed to index document %s: %s", doc_id, str(e), exc_info=True)
        raise  # Re-raise so outer handler can catch it


def lambda_handler(event, context):
    """
    Lambda function to index photos uploaded to S3.
    Triggered by S3 PUT events.
    """
    logger.info("Received event: %s", json.dumps(event))
    
    # Validate environment variables
    if not OS_ENDPOINT:
        logger.error("OS_ENDPOINT environment variable not set")
        return {
            'statusCode': 500,
            'body': json.dumps('ElasticSearch host not configured')
        }
    
    # S3 PUT event may contain multiple records; handle each
    for rec in event.get("Records", []):
        try:
            s3rec = rec.get("s3", {})
            bucket = s3rec["bucket"]["name"]
            key = s3rec["object"]["key"]
            
            # S3 event keys are URL-encoded. Decode to get the actual object key.
            # Use unquote_plus to handle both % encoding and + as space.
            decoded_key = urllib.parse.unquote_plus(key, encoding='utf-8')
            
            logger.info("Raw S3 event key: %s", key)
            logger.info("Decoded key for S3 operations: %s", decoded_key)
            logger.info("Processing s3://%s/%s", bucket, decoded_key)
            
            # Use decoded_key for all S3 operations
            rek_labels = get_rekognition_labels(bucket, decoded_key)
            custom_labels = get_custom_labels(bucket, decoded_key)
            
            # Merge and normalize all labels
            merged = normalize_labels(custom_labels + rek_labels)
            logger.info("Merged labels: %s", merged)
            
            # Get timestamp (ISO8601 UTC format)
            timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds') + "Z"
            
            # Create document for ElasticSearch
            doc = {
                "objectKey": decoded_key,  # Store the decoded key
                "bucket": bucket,
                "createdTimestamp": timestamp,
                "labels": merged
            }
            
            # Use decoded key as doc id (URL-safe). Replace / with _ for valid doc IDs
            doc_id = decoded_key.replace("/", "_")
            
            # Index the document
            res = index_document(doc_id, doc)
            logger.info("Indexed doc: %s", res)
            
        except Exception as e:
            logger.exception("Error processing record: %s", e)
            # Continue processing other records even if one fails
    
    return {"status": "ok"}
