"""유튜브 멀티채널 예약 업로드.

MultiLogin 프로필마다 하나의 채널을 맡기고, 예약한 시각에 맞춰
제목 / 설명 / 해시태그 / (가능한 채널이면) 쇼핑 제품 태그까지 채워 올립니다.

  python src/upload_app.py          # 앱(로컬 웹 UI) 실행
  python -m upload.worker --once    # UI 없이 큐만 한 번 비우기
"""
