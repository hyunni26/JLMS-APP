import re

from aws_config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_BUCKET, AWS_DB_PREFIX
from aws_sigv4 import s3_request


def list_db_backups():
    """S3에 올라간 DB 백업 목록을 최신순으로 반환한다. 반환: (성공여부, [{key, last_modified}, ...] 또는 에러메시지)"""
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        return False, "AWS 키가 설정되어 있지 않습니다. Render의 Environment Variables에서 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY를 확인해주세요."
    try:
        status, resp = s3_request(
            "GET", AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_BUCKET, "",
            query_params={"list-type": "2", "prefix": f"{AWS_DB_PREFIX}/"},
        )
        if status != 200:
            return False, f"목록 조회 실패 (status {status}): {resp.decode('utf-8', errors='ignore')[:300]}"
        xml_text = resp.decode("utf-8", errors="ignore")
        entries = []
        for m in re.finditer(r"<Contents>(.*?)</Contents>", xml_text, re.DOTALL):
            block = m.group(1)
            key_m = re.search(r"<Key>(.*?)</Key>", block)
            date_m = re.search(r"<LastModified>(.*?)</LastModified>", block)
            if key_m and date_m:
                entries.append({"key": key_m.group(1), "last_modified": date_m.group(1)})
        entries.sort(key=lambda e: e["last_modified"], reverse=True)
        return True, entries
    except Exception as e:
        return False, f"목록 조회 중 오류가 발생했습니다: {e}"


def download_db_backup(key, save_path):
    """S3에서 특정 DB 백업(key)을 save_path로 받는다. 반환: (성공여부, 메시지)"""
    try:
        status, resp = s3_request("GET", AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_BUCKET, key)
        if status != 200:
            return False, f"다운로드 실패 (status {status}): {resp.decode('utf-8', errors='ignore')[:300]}"
        with open(save_path, "wb") as f:
            f.write(resp)
        return True, f"다운로드 완료"
    except Exception as e:
        return False, f"다운로드 중 오류가 발생했습니다: {e}"
