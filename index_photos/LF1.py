import os
import json
import logging
from datetime import datetime
import boto3
import requests
from requests_aws4auth import AWS4Auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables with defaults
REKOGNITION_MIN_CONF = float(os.environ.get("REKOGNITION_MIN_CONF", "70.0"))  # percent
OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST")  # e.g. https://search-photos-abc123.us-east-1.es.amazonaws.com
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "photos")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Initialize AWS clients
s3 = boto3.client("s3")
rek = boto3.client("rekognition")

# Initialize AWS4Auth for ElasticSearch requests (created once at module level)
session = boto3.session.Session()
credentials = session.get_credentials().get_frozen_credentials()
awsauth = AWS4Auth(
    credentials.access_key,
    credentials.secret_key,
    REGION,
    "es",
    session_token=credentials.token
)


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
    url = f"{OPENSEARCH_HOST}/{OPENSEARCH_INDEX}/_doc/{doc_id}"
    headers = {"Content-Type": "application/json"}
    r = requests.put(url, auth=awsauth, headers=headers, data=json.dumps(body))
    if r.status_code not in (200, 201):
        logger.error("Failed to index doc: status=%s resp=%s", r.status_code, r.text)
        raise Exception(f"Index error: {r.status_code} {r.text}")
    return r.json()


def lambda_handler(event, context):
    """
    Lambda function to index photos uploaded to S3.
    Triggered by S3 PUT events.
    """
    logger.info("Received event: %s", json.dumps(event))
    
    # Validate environment variables
    if not OPENSEARCH_HOST:
        logger.error("OPENSEARCH_HOST environment variable not set")
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
            
            # Key may be URL-encoded in event, decode if necessary
            key = requests.utils.unquote(key)
            
            logger.info("Processing s3://%s/%s", bucket, key)
            
            # Get Rekognition labels
            rek_labels = get_rekognition_labels(bucket, key)
            
            # Get custom labels from S3 metadata
            custom_labels = get_custom_labels(bucket, key)
            
            # Merge and normalize all labels
            merged = normalize_labels(custom_labels + rek_labels)
            
            # Get timestamp (ISO8601 UTC format)
            timestamp = datetime.utcnow().isoformat() + "Z"
            
            # Create document for ElasticSearch
            doc = {
                "objectKey": key,
                "bucket": bucket,
                "createdTimestamp": timestamp,
                "labels": merged
            }
            
            # Use object key as doc id (URL-safe). Replace / with _ for valid doc IDs
            doc_id = key.replace("/", "_")
            
            # Index the document
            res = index_document(doc_id, doc)
            logger.info("Indexed doc: %s", res)
            
        except Exception as e:
            logger.exception("Error processing record: %s", e)
            # Continue processing other records even if one fails
    
    return {"status": "ok"}
