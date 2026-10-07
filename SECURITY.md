# 보안 정책

Resource Guard는 단일 소유자의 개인 WSL과 Tailnet용 관리 도구입니다.
Tailscale 소유자 검증, 제한된 Unix 소켓, Origin·CSRF 검증을 끄거나 공개 인터넷에 노출하지 마세요.

취약점은 GitHub 저장소의 Security → Advisories → Report a vulnerability로 비공개 제보해주세요.
공개 Issue에 토큰·비밀번호·실제 로그·개인 Tailnet 주소를 첨부하지 마세요.
재현 버전, 예상 권한, 비밀이 아닌 fixture로 만든 최소 재현을 포함하면 도움이 됩니다.

초기에는 최신 공개 릴리스에 보안 수정을 제공합니다. 지원 범위 변경은 CHANGELOG에 기록합니다.
root 컨트롤러의 허용 연산, 사용자 파일 접근과 설치 권한 경계가 보안 검토의 핵심입니다.
