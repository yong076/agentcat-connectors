# Agent Cat Connectors

[English](README.md) | [한국어](README.ko.md)

Agent Cat Connectors는 Agent Cat 메뉴바 앱이 이 Mac에서 실행 중인 Codex, Claude Code, Gemini CLI 활동을 로컬에서 읽을 수 있게 해주는 커넥터입니다.

작은 로컬 수집기를 설치하고, 데이터는 `~/.agentcat` 아래에 보관합니다. 지원되는 CLI 설정에는 Agent Cat이 관리하는 hook/telemetry 항목을 추가해서 이후 세션부터 활동과 사용량을 보고할 수 있게 합니다. 프롬프트 본문은 원격 서버로 보내지 않습니다.

## 설치

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.ps1 | iex
```

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.sh | bash
```

클론한 체크아웃에서 실행할 때:

```bash
./install.sh
```

Windows에서 클론한 체크아웃을 실행할 때:

```powershell
.\install.ps1
```

설치 후 확인:

```powershell
agentcat snapshot
```

설치가 끝난 뒤 Codex, Claude Code, Gemini CLI에 붙여 넣을 수 있는 설정 프롬프트를 복사하려면:

```bash
agentcat setup-prompt
```

## 설치되는 항목

- `~/.local/bin/agentcat` 또는 `%USERPROFILE%\.local\bin\agentcat.cmd`: 로컬 수집기 CLI
- `~/Library/LaunchAgents/com.trappist.agentcatd.plist` 또는 Windows 시작 작업 `AgentCatD`: `127.0.0.1:8765`에서 동작하는 로컬 daemon
- `~/.agentcat/events.sqlite`: 로컬 이벤트 저장소
- `~/.agentcat/latest-snapshot.json`: 최신 정규화 사용량 스냅샷
- `~/.agentcat/backups/`: 설정 변경 전 timestamp 백업

## 지원 Provider

| Provider | 신호 | 비고 |
| --- | --- | --- |
| Codex | 로컬 SQLite token 합계 + Codex OAuth usage API | `~/.codex/auth.json`이 있으면 5시간, 7일, 노출된 모델/리뷰 quota의 남은 비율을 표시합니다. |
| Claude Code | 로컬 stats/hooks + Claude Code OAuth usage API | Claude Code OAuth credential이 있으면 5시간, 7일, 모델 quota, 월별 extra credit 남은 양을 표시합니다. |
| Gemini CLI | 로컬 telemetry + Gemini Code Assist quota API | Google 로그인 Gemini CLI 세션에서 모델별 Code Assist request quota 남은 비율을 표시합니다. |
| GitHub Copilot | 로컬 session-state + VS Code transcript | legacy 이벤트는 실제 token 수를 담고, VS Code transcript 경로는 문자 수 기반 추정이라 해당 스냅샷은 `estimated: true`를 설정합니다. |
| Cursor | 로컬 `state.vscdb` bubble token 수 | `tokenCount`가 있으면 실제 값을 쓰고, 없는 bubble은 문자 수로 추정하며 `estimated: true`를 설정합니다. |
| Goose | 로컬 `sessions.db` 누적 token | 세션별 실제 input/output token 합계입니다. |
| Kiro | 로컬 `.chat` 파일 | 디스크에 실제 token 수가 없어 전부 문자 수 기반 추정이며, 스냅샷은 항상 `estimated: true`를 설정합니다. |
| Roo Code | 로컬 cline 계열 task 로그 | 요청별 실제 `tokensIn`/`tokensOut`(+ cache read/write)입니다. |
| Kilo Code | 로컬 cline 계열 task 로그 | 요청별 실제 `tokensIn`/`tokensOut`(+ cache read/write)입니다. |
| Cline | 로컬 cline 계열 task 로그 | 요청별 실제 `tokensIn`/`tokensOut`(+ cache read/write)입니다. |
| Qwen Code | 로컬 chat `usageMetadata` | 실제 prompt/candidate/thought/cached token 수입니다. |
| Crush | 로컬 `crush.db` session store | root session별 실제 prompt/completion token 합계입니다. |
| Continue | 로컬 `dev_data/tokensGenerated.jsonl` | 생성마다 실제 prompt/generated token 수입니다. |
| PearAI | 로컬 `dev_data/tokensGenerated.jsonl` (Continue fork) | 생성마다 실제 prompt/generated token 수입니다. |
| llm (simonw) | 로컬 `logs.db` responses 테이블 | response별 실제 input/output token 수입니다. |
| gptme | 로컬 `conversation.jsonl` usage | token 필드가 있으면 실제 값을 쓰고, 추정은 하지 않으며 없으면 `no_token_events_yet`로 보고합니다. |

로컬 사용량 provider는 IDE/프로세스 존재 여부가 아니라 디스크에 남는 영속 데이터만 읽습니다. 디스크에 실제 token 수가 없는 provider는 문자 수로 추정하고 해당 스냅샷에 `provider.estimated = true`(아니면 생략/false)를 설정해, 앱이 그 수치를 근사값으로 표시할 수 있게 합니다. 현재 추정 기반: Cursor(fallback bubble), Kiro(항상), Copilot의 VS Code transcript 경로입니다.

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

Agent Cat은 `~/.agentcat/latest-snapshot.json`을 읽거나 로컬 API를 호출할 수 있습니다. 스냅샷에는 provider별 사용량과 함께 `activity.processes`, `activity.countsByProvider`, `activity.totalCPUPercent`, `activity.totalMemoryBytes`, `activity.memoryBytesByProvider`, `activity.runnableProcessCount`, `activity.activityScore`, `activity.motionStage`가 들어 있습니다. 그래서 sandboxed Mac 빌드도 직접 process scan을 하지 않고 커넥터를 통해 활동 상태를 받을 수 있습니다.

`activity.motionStage`는 단순 에이전트 갯수가 아니라 현재 활동량으로 계산합니다. 커넥터는 `totalCPUPercent + runnableProcessCount * 4`를 활동 점수로 사용하고 앱과 같은 4단계만 내보냅니다. 에이전트 프로세스가 없으면 `sleeping`, 프로세스가 있지만 대부분 대기 중이면 `walking`, 7점부터 `running`, 22점부터 `sprinting`입니다.

메모리 사용량은 `/bin/ps`에서 읽은 로컬 RSS 메모리입니다. 각 프로세스에는 `memoryBytes`, provider별 합계에는 `memoryBytesByProvider`가 들어갑니다. 프롬프트, 대화 본문, 모델 응답은 읽지 않습니다.

`activity.runtimeModes`는 고강도 에이전트 세션을 위한 선택적 로컬 전용 신호입니다. Claude Code `UserPromptSubmit` hook은 `ultrathink` / `ultracode`를 메모리 안에서만 감지하고 프롬프트 원문은 버린 뒤, `mode=ultrathink`, `confidence=exact`, `privacy=prompt_text_discarded` 같은 짧은 수명의 플래그만 저장합니다. `effort.level=xhigh`나 Codex `model_reasoning_effort=xhigh` 같은 메타데이터 신호는 프롬프트나 transcript를 읽지 않고 `mode=effort_xhigh`로 정규화합니다. Claude `Stop` hook이 오면 플래그를 지웁니다. 프롬프트, 파일 경로, transcript, 대화 본문은 저장하지 않습니다.

Windows에서는 가능한 경우 PowerShell 7(`pwsh`)을 우선 사용하고, 빠른 `Get-Process` 스캔을 먼저 시도한 뒤 필요할 때만 command-line 스캔이나 `tasklist`로 fallback합니다. 회사 PC처럼 PowerShell 기동이 느린 환경에서는 `~/.agentcat/settings.json`에서 스캔 타임아웃을 늘릴 수 있습니다.

```json
{
  "windowsProcessScanTimeoutSeconds": 8
}
```

## 한도

Agent Cat은 각 CLI가 이미 로그인에 쓰는 로컬 인증 상태로 provider가 노출하는 남은 quota를 읽습니다.

- Codex: `~/.codex/auth.json`을 읽고 ChatGPT Codex usage endpoint를 호출해 5시간/7일 rolling window 사용률과 reset 시간을 가져옵니다.
- Claude Code: macOS Keychain 또는 `~/.claude`의 Claude Code OAuth credential을 읽고 Claude Code OAuth usage endpoint를 호출해 5시간/7일/model 사용률과 월별 extra usage credit을 가져옵니다.
- Gemini CLI: `~/.gemini/oauth_creds.json`과 `~/.gemini/settings.json`을 읽고 Gemini Code Assist `loadCodeAssist`, `retrieveUserQuota`를 호출해 모델별 request quota 남은 비율과 reset 시간을 가져옵니다.
- Fallback: live quota 조회가 실패하면 Codex/Claude는 가능한 경우 최신 local status-line 또는 session `token_count` 이벤트를 사용합니다.

정규화된 스냅샷에는 `providers.<name>.limits.quotas[]`가 들어갑니다. 각 quota entry는 `remaining` 또는 `remainingPercent`를 우선 제공하고, progress bar용 `usedPercent`, reset 표시용 `resetAt`을 포함합니다. 일부 provider는 절대 token/request 수가 아니라 비율만 노출합니다. Agent Cat은 그런 값을 추정하지 않고 unavailable로 둡니다.

누락된 한도나 직접 관리하고 싶은 token cap은 `~/.agentcat/limits.json`에 넣으면 됩니다. 수동 설정값은 호환되는 token cap에 우선 적용되고, live quota 카드는 계속 표시됩니다.

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
