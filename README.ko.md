# Agent Cat Connectors

[English](README.md) | [한국어](README.ko.md)

Agent Cat Connectors는 Agent Cat 메뉴바 앱이 이 Mac에서 실행 중인 Codex, Claude Code, Gemini CLI 활동을 로컬에서 읽을 수 있게 해주는 커넥터입니다.

작은 로컬 수집기를 설치하고, 데이터는 `~/.agentcat` 아래에 보관합니다. 지원되는 CLI 설정에는 Agent Cat이 관리하는 hook/telemetry 항목을 추가해서 이후 세션부터 활동과 사용량을 보고할 수 있게 합니다. 프롬프트 본문은 원격 서버로 보내지 않습니다.

## 설치

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.sh | bash
```

클론한 체크아웃에서 실행할 때:

```bash
./install.sh
```

설치 후 확인:

```bash
agentcat snapshot
```

설치가 끝난 뒤 Codex, Claude Code, Gemini CLI에 붙여 넣을 수 있는 설정 프롬프트를 복사하려면:

```bash
agentcat setup-prompt
```

## 설치되는 항목

- `~/.local/bin/agentcat`: 로컬 수집기 CLI
- `~/Library/LaunchAgents/com.trappist.agentcatd.plist`: `127.0.0.1:8765`에서 동작하는 로컬 daemon
- `~/.agentcat/events.sqlite`: 로컬 이벤트 저장소
- `~/.agentcat/latest-snapshot.json`: 최신 정규화 사용량 스냅샷
- `~/.agentcat/backups/`: 설정 변경 전 timestamp 백업

## 지원 Provider

| Provider | MVP 신호 | 비고 |
| --- | --- | --- |
| Codex | 로컬 SQLite token 합계 + 선택적 notify hook | Codex CLI는 정확한 weekly/monthly quota 절대값을 로컬에 노출하지 않습니다. |
| Claude Code | `stats-cache.json`, status line 입력, hooks | 로컬 stats가 있으면 사용하고, 이후 status-line/hook payload를 수집합니다. |
| Gemini CLI | 로컬 telemetry 파일 | Gemini가 로컬 telemetry를 켠 상태로 실행된 뒤 token 데이터가 나타납니다. |

## 개인정보

커넥터는 local-first로 설계되어 있습니다.

- 프롬프트 본문은 의도적으로 저장하지 않습니다.
- Claude/Gemini hook payload는 저장 전에 재귀적으로 sanitize합니다.
- 로컬 daemon은 `127.0.0.1`에서만 listen합니다.
- 이 repo는 아무 데이터도 업로드하지 않습니다. 서버 동기화는 이후 제품 단계입니다.

## HTTP API

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/v1/snapshot
```

Agent Cat은 `~/.agentcat/latest-snapshot.json`을 읽거나 로컬 API를 호출할 수 있습니다. 스냅샷에는 provider별 사용량과 함께 `activity.processes`, `activity.countsByProvider`, `activity.totalCPUPercent`, `activity.runnableProcessCount`, `activity.activityScore`, `activity.motionStage`가 들어 있습니다. 그래서 sandboxed Mac 빌드도 직접 process scan을 하지 않고 커넥터를 통해 활동 상태를 받을 수 있습니다.

`activity.motionStage`는 단순 에이전트 갯수가 아니라 현재 활동량으로 계산합니다. 커넥터는 `totalCPUPercent + runnableProcessCount * 3`을 활동 점수로 사용합니다. `jogging`은 5점부터, `running`은 25점부터, `sprinting`은 60점부터 시작합니다.

## 한도

Agent Cat은 로컬 에이전트 런타임이 이미 노출하는 quota 데이터를 자동 감지합니다.

- Codex: `~/.codex/sessions/**/rollout-*.jsonl`의 최신 `token_count` 이벤트
- Claude Code: Agent Cat hook이 캡처한 최신 `claude-statusline` payload

이 소스들은 rolling-window 사용률과 model context size를 노출할 수 있습니다. 하지만 모든 provider의 절대 한도값을 주지는 않습니다. Gemini CLI도 현재 신뢰할 수 있는 로컬 quota payload를 제공하지 않습니다.

즉, Agent Cat은 정확히 관측 가능한 값만 자동으로 표시합니다. 누락된 absolute limit이나 직접 관리하고 싶은 한도는 `~/.agentcat/limits.json`에 넣으면 됩니다. 수동 설정값은 자동 감지값보다 우선합니다.

예시:

```json
{
  "providers": {
    "codex": {
      "week": 1000000000,
      "month": 4000000000,
      "session": 200000
    },
    "claude": {
      "week": 500000000,
      "month": 2000000000,
      "session": 200000
    },
    "gemini": {
      "week": 500000000,
      "month": 2000000000,
      "session": 1000000
    }
  }
}
```

## 제거

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/uninstall.sh | bash
```

클론한 체크아웃에서 실행할 때:

```bash
./uninstall.sh
```

제거 스크립트는 LaunchAgent, binary link, Agent Cat이 관리한 config 항목을 제거합니다. `~/.agentcat` 아래의 로컬 사용량 데이터는 직접 지우기 전까지 유지됩니다.

## 개발

```bash
python3 -m py_compile bin/agentcat scripts/install.py
bin/agentcat snapshot --json
```

## Codex Skill

이 repo에는 `skills/agentcat-usage/SKILL.md`도 포함되어 있습니다. Codex 계열 에이전트가 로컬 사용량을 추측하지 않고 `agentcat snapshot --json`을 호출하도록 가르치는 용도입니다.
