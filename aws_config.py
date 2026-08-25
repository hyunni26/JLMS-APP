"""
AWS S3 백업 설정. 실제 값은 Render 환경변수(Environment)에서 관리하고,
코드에는 절대 하드코딩하지 않는다 (GitHub push protection 및 키 노출 방지).
"""
import os

AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

AWS_REGION = "ap-northeast-2"  # 서울
AWS_BUCKET = "jmo-lms"

AWS_DB_PREFIX = "JMO_LMS_DB"    # DB 백업 저장 폴더
AWS_EXE_PREFIX = "JMO_LMS_EXE"  # 프로그램 업데이트(exe) 폴더
