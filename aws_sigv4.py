"""
AWS S3용 최소 SigV4(Signature Version 4) 서명 구현.
boto3 없이 표준 라이브러리(hashlib/hmac/urllib)만으로 S3 PUT/GET을 직접 호출한다.
"""

import hashlib
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key, datestamp, region, service):
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


def s3_request(method, access_key, secret_key, region, bucket, key, body=b"", query_params=None):
    """S3에 서명된 요청(PUT/GET/목록조회)을 직접 보낸다. 반환: (status_code, response_bytes)
    query_params을 주면 (예: 버킷 목록조회) 쿼리스트링도 서명에 포함해서 요청한다."""
    service = "s3"
    host = f"{bucket}.s3.{region}.amazonaws.com"

    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_uri = "/" + urllib.request.quote(key, safe="/-_.~") if key else "/"

    if query_params:
        sorted_items = sorted(query_params.items())
        canonical_querystring = "&".join(
            f"{urllib.request.quote(k, safe='')}={urllib.request.quote(v, safe='')}" for k, v in sorted_items
        )
    else:
        canonical_querystring = ""

    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amzdate}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join(
        [method, canonical_uri, canonical_querystring, canonical_headers, signed_headers, payload_hash]
    )

    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256", amzdate, credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = _signing_key(secret_key, datestamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    headers = {
        "x-amz-date": amzdate,
        "x-amz-content-sha256": payload_hash,
        "Authorization": authorization,
    }

    url = f"https://{host}{canonical_uri}"
    if canonical_querystring:
        url += f"?{canonical_querystring}"

    req = urllib.request.Request(url, data=body if method == "PUT" else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
