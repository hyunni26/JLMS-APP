import re

from aws_config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_BUCKET, AWS_DB_PREFIX
from aws_sigv4 import s3_request

# 기존 통합 lens_manager.db가 역할별로 6개 파일로 분리됨.
# S3 경로 패턴: {AWS_DB_PREFIX}/{db_name}/{db_name}_{타임스탬프}.db
DB_NAMES = ["main", "company_master", "hardroom", "coatingroom", "rx", "packing"]


def list_db_backups(db_name):
    """S3에서 특정 db_name 폴더의 백업 목록을 최신순으로 반환한다.
    반환: (성공여부, [{key, last_modified}, ...] 또는 에러메시지)"""
    try:
        status, resp = s3_request(
            "GET", AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_BUCKET, "",
            query_params={"list-type": "2", "prefix": f"{AWS_DB_PREFIX}/{db_name}/"},
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
        return True, "다운로드 완료"
    except Exception as e:
        return False, f"다운로드 중 오류가 발생했습니다: {e}"


def sync_all_dbs(save_paths):
    """6개 DB 파일을 각각 최신 백업으로 동기화한다.
    save_paths: {db_name: 저장경로} 딕셔너리 (DB_NAMES와 동일한 키 사용)
    반환: (전체성공여부, {db_name: (성공여부, 메시지 또는 최신 LastModified)})
    각 파일은 서로 다른 시각에 백업될 수 있으므로 개별적으로 처리하고,
    하나가 실패해도 나머지는 계속 진행한다."""
    results = {}
    all_ok = True
    for name in DB_NAMES:
        ok, entries = list_db_backups(name)
        if not ok:
            results[name] = (False, entries)
            all_ok = False
            continue
        if not entries:
            results[name] = (False, "서버에 저장된 백업이 없습니다.")
            all_ok = False
            continue
        latest = entries[0]
        ok2, msg = download_db_backup(latest["key"], save_paths[name])
        if ok2:
            results[name] = (True, latest["last_modified"])
        else:
            results[name] = (False, msg)
            all_ok = False
    return all_ok, results
