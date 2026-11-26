# AcceptTC for VS Code

AcceptTC 가 설치된 프로젝트(`.accepttc/` 폴더가 있는 프로젝트)에서 테스트 케이스 검증 현황을 VS Code 안에서 봅니다. 서버를 띄우지 않고, 모델도 호출하지 않습니다.

- 상태 표시줄: `TC 승인 수/전체 수`. 클릭하면 검증 현황이 열립니다.
- 명령 팔레트: `AcceptTC: 검증 현황 열기`, `AcceptTC: 검증 현황 새로 고침`, `AcceptTC: 설치 상태 점검`
- 검증이 기록되거나 테스트·근거 기록 파일을 저장하면 화면이 자동으로 갱신됩니다.
- 화면의 파일 이름(`tests/foo_test.cpp:12`, `docs/requirements.md:10`)을 누르면 편집기에서 그 줄이 열립니다.

## 설치

```bash
code --install-extension accepttc-0.1.0.vsix
```

WSL 이나 원격 환경에서는 프로젝트가 있는 쪽(WSL/원격)에 설치하세요. `python3` 가 필요하며, 다른 명령을 쓰려면 설정 `accepttc.python` 을 바꾸면 됩니다.
