"""
AWS S3 백업 설정. GitHub에 올라가는 코드라 실제 키값은 여기 적지 않고,
Render의 "Environment Variables"에서 넣어준 값을 읽어온다.
"""

import os

AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

AWS_REGION = "ap-northeast-2"  # 서울
AWS_BUCKET = "jmo-lms"

AWS_DB_PREFIX = "JMO_LMS_DB"    # DB 백업 저장 폴더
AWS_EXE_PREFIX = "JMO_LMS_EXE"  # 프로그램 업데이트(exe) 폴더

