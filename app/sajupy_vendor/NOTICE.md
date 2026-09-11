# 이 디렉터리에 대해

원본: https://github.com/0ssw1/sajupy (MIT License, Copyright (c) 2025 0ssw1)

로컬 개발 환경에서 PyPI 접근이 막혀 있어 `pip install sajupy` 대신
GitHub 소스를 그대로 vendor(복사)해서 프로젝트에 포함시켰습니다.

원본 대비 변경 사항 (core.py):
- `geopy`가 설치되어 있지 않은 환경에서도 동작하도록, 주요 도시
  경도를 내장한 `KNOWN_CITY_LONGITUDES` 딕셔너리를 추가하고
  `geopy` import를 optional(try/except)로 변경했습니다.
- 계산 로직(만세력 변환, 절기, 태양시 보정 등) 자체는 수정하지 않았습니다.

실제 배포 환경에서 PyPI 접근이 가능하고 geopy까지 설치할 수 있다면,
`pip install sajupy geopy`로 원본 패키지를 그대로 쓰고 이 vendor
디렉터리는 참고용으로만 남겨둬도 됩니다.
