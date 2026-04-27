# 미국 큰 목사 설교 자동화 — 최종 플랜

**목표:** 1주일에 저코스트로 최대한 많은 질 좋은 기사 뽑아내기. 비용 최소화 + 대량 기사.

---

## 전체 파이프라인 흐름

```
Create Article 클릭
↓
Collect recent videos from registered pastor channels
↓
Remove already-published and failed videos
↓
Score candidates
↓
Try cheap transcript extraction in parallel
↓
Use first successful transcript
↓
Extract primary scripture + strong quotes
↓
Summarize sermon with Gemini Flash-Lite
↓
Generate either:
  - News Article
  - Devotional Blog
↓
Run SEO Scorer
↓
Run Risk Reviewer
↓
Show preview in admin
↓
Publish static article
↓
Save metadata, cost, source, and status to DB
```

---

## 1. YouTube API — 채널 수집

YouTube API = 영상 검색/관리용으로 필요함.

- 최근 설교 영상 찾기
- 영상 제목 확인
- 영상 날짜 확인
- 채널 확인
- 영상 ID 가져오기
- 썸네일 가져오기
- 영상 길이 확인
- 중복 영상인지 확인

**Source Collector 구조:**

이미 등록된 채널 ID 기반으로 수집한다.

```
Known Pastor Channels
↓
RSS 또는 uploads playlist에서 최근 영상 ID 수집
↓
videos.list로 title / date / duration / thumbnail 한 번에 보강
↓
필요할 때만 search.list 사용
```

---

## 2. 자막 추출 (Transcript)

> 제일 중요. 여기서 실패하면 이후 모든 단계가 멈춤.

### 자막 유형별 추출 가능 여부

| 자막 유형 | 추출 가능 여부 | 사용 도구 |
|---|---|---|
| 수동 업로드 자막 (SRT/VTT) | ✅ 가능 | yt-dlp / youtube-transcript-api |
| 자동생성 자막 (Auto-caption) | ✅ 가능 | yt-dlp / youtube-transcript-api |
| 자막 없는 영상 | ⚠️ 조건부 가능 | Whisper AI (음성인식 폴백) |

### 실시간 기사 생성 경로

```
youtube-transcript-api
→ yt-dlp subtitle only
→ Supadata top 3 fallback
→ 실패하면 skip
```

### 비실시간 보강 경로

Whisper는 background job으로만 실행.
1분 안에 기사 생성하려면 Whisper는 기본 OFF.
"이 영상 꼭 써야 한다"는 경우에만 백그라운드에서 돌린다.

### Transcript 상세 흐름

```
Create Article 클릭
↓
여러 채널에서 후보 영상 수집
↓
이미 실패한 영상 DB에서 제거
↓
후보 영상 점수화
↓
상위 20개 선택
↓
cheap transcript 병렬 시도
↓
성공하면 즉시 사용
↓
실패하면 상위 3개만 Supadata fallback
↓
성공하면 summary/article 생성
↓
실패하면 "usable transcript 없음" 반환
```

### Transcript 시간 제한 (코드에 명시 필요)

| 항목 | 값 |
|---|---|
| Transcript extraction max time | 20 seconds |
| Candidate videos | 20 |
| Parallel transcript attempts | 5 at a time |
| Supadata fallback | top 3 only |
| Whisper | disabled in real-time mode |

---

## 3. 이미지

기사 및 블로그용 이미지 = **YouTube 썸네일 다운로드**.

---

## 4. 설교 요약 — Gemini 2.5 Flash Lite

자막이 추출되면 Gemini 2.5 Flash Lite를 사용해서 설교를 요약.

- 키워드 추출 담당
- 설교 요약 담당

---

## 5. 기사 작성 — ChatGPT 4.1 mini

요약한 것을 가지고 ChatGPT 4.1 mini로 기사 작성.

- Gemini가 요약한 내용 + 키워드를 받아서 작성
- 2가지 모드: **블로그용 / 기사용**

---

## 6. 기사 Publish

Publish 위치는 추후 결정.

---

## 영상 점수화 기준

### 🔴 필수 기준 (Pass/Fail)

| 기준 | 이유 |
|---|---|
| 자막 존재 여부 | 없으면 Whisper 비용 폭발 |
| 영상 길이 10분 이상 | 너무 짧으면 기사 쓸 내용이 없음 |
| 영상 길이 2시간 이하 | 너무 길면 transcript 처리 비용 큼 |
| 영어 자막 여부 | 번역 기사가 아니라면 필수 |

### 🟡 점수 항목

**① 최신성 (30점)**

| 기간 | 점수 |
|---|---|
| 7일 이내 | 30점 |
| 14일 이내 | 20점 |
| 30일 이내 | 10점 |
| 그 이상 | 0점 |

**② 조회수 / 채널 규모 (25점)**

| 조회수 | 점수 |
|---|---|
| 50만+ | 25점 |
| 10만+ | 15점 |
| 1만+ | 5점 |

※ 채널 구독자 대비 조회수 비율도 함께 참고.

**③ 자막 품질 (20점)**

| 자막 종류 | 점수 |
|---|---|
| 수동 업로드 자막 | 20점 |
| 자동생성 자막 | 10점 |
| 자막 없음 (Whisper 필요) | 0점 |

**④ 설교 길이 (15점)**

| 길이 | 점수 |
|---|---|
| 25~45분 | 15점 |
| 45~70분 | 10점 |
| 10~25분 | 5점 |
| 70분 이상 | 3점 |

**⑤ 신규 영상 (10점)**

DB에 없는 새 영상이면 +10점. 중복 방지 겸 다양성 확보.

### 🟢 보너스 점수

| 항목 | 점수 |
|---|---|
| 댓글 수 많음 | +5점 |
| 성경 구절이 제목/설명에 명시됨 | +5점 |
| 시리즈 설교 중 1편 | +3점 |

### 적용 예시

```
John MacArthur - "The Sovereignty of God" (3일 전, 조회수 8만, 수동자막, 38분)
→ 최신성 30 + 조회수 15 + 자막 20 + 길이 15 + 신규 10 = 90점 ✅

David Jeremiah - "Hope" (45일 전, 조회수 500, 자동자막, 22분)
→ 최신성 0 + 조회수 0 + 자막 10 + 길이 5 + 신규 10 = 25점 ❌
```

---

## 핵심 기능들

### Admin View

- 각 기사당 Gemini / OpenAI 사용량 cost 표기 (admin view에서만)
- 한 기사에 포함되어야 할 것들: 키워드, 제목, 요약문, 날짜, Risk, SEO 점수, 본문, Sources, Reviewer Note

### 목사 채널 등록

- 목사님들 채널 추가할 수 있는 form이 있어야 함

---

## Article Generator

### 입력값 (Input JSON)

```json
{
  "keyword": "john piper sermon",
  "pastor_name": "",
  "church_or_ministry": "",
  "sermon_title": "",
  "video_url": "",
  "published_date": "",
  "sources": [],
  "transcript": "",
  "transcript_quality": "manual | auto | fallback",
  "primary_scripture": "Philippians 3:8-11",
  "strong_quotes": [],
  "tone": "Christian news editorial",
  "word_count": 500,
  "article_mode": "news | blog"
}
```

### 출력값 (Output JSON)

```json
{
  "title": "",
  "deck": "",
  "article_body": "",
  "primary_scripture": "",
  "seo_title": "",
  "meta_description": "",
  "tags": []
}
```

---

## SEO Scorer

검사 항목:
- 제목에 키워드 포함
- 첫 문단에 키워드 포함
- meta description 존재
- 본문 길이 적절
- source 수 충분
- heading 구조 있음
- 중복 표현 적음

출력 예시: `SEO Score: 84/100`

---

## Risk Reviewer

### 검토 항목

1. 출처에 없는 주장 했는가?
2. 목회자 발언을 과장했는가?
3. 정치적/사회적 논쟁 표현이 강한가?
4. 설교 내용을 왜곡했는가?
5. 성경구절을 잘못 연결했는가?
6. 원문과 너무 비슷한 문장을 썼는가?

### 입력값

```
1. Original transcript summary
2. Extracted quotes
3. Final article
4. Source metadata
5. Primary scripture
```

### 출력 JSON

```json
{
  "risk_level": "LOW",
  "status": "PASS",
  "reviewer_notes": [],
  "unsupported_claims": [],
  "quote_accuracy": "PASS",
  "scripture_accuracy": "PASS"
}
```

### 출력 예시

```
Risk: LOW
✓ PASS
```

또는:

```
Risk: MEDIUM
✗ REVIEW

Reviewer Note:
- The article makes one unsupported claim about the pastor's political view.
- The connection between the sermon and current events needs verification.
```

---

## Static Article Generator

### 파일 구조

```
/articles/
  20260427_120501_john_piper_sermon/
    article.html
    metadata.json
    image.jpg
/data/
  articles-index.json
```

메인 페이지는 `articles-index.json`을 읽어서 렌더링.

---

## DB Schema

### channels

| 컬럼 | 타입 |
|---|---|
| id | — |
| pastor_name | — |
| channel_id | — |
| channel_title | — |
| is_active | — |
| created_at | — |

### videos

| 컬럼 | 타입 |
|---|---|
| id | — |
| youtube_video_id | — |
| channel_id | — |
| title | — |
| published_at | — |
| duration_seconds | — |
| view_count | — |
| thumbnail_url | — |
| transcript_status | — |
| score | — |
| failure_reason | — |
| created_at | — |

### articles

| 컬럼 | 타입 |
|---|---|
| id | — |
| video_id | — |
| mode | — |
| title | — |
| slug | — |
| primary_scripture | — |
| seo_score | — |
| risk_level | — |
| status | — |
| html_path | — |
| total_cost | — |
| created_at | — |
| published_at | — |

### article_costs

| 컬럼 | 타입 |
|---|---|
| id | — |
| article_id | — |
| model_name | — |
| input_tokens | — |
| output_tokens | — |
| estimated_cost | — |

### failed_transcripts

| 컬럼 | 타입 |
|---|---|
| id | — |
| youtube_video_id | — |
| reason | — |
| last_attempted_at | — |
| retry_after | — |

---

## MVP 범위

### ✅ MVP 1단계

1. Pastor channel ID 등록
2. 최근 영상 후보 수집
3. Transcript 가능한 영상만 필터링
4. 상위 후보 점수화
5. Gemini로 요약
6. GPT로 기사 생성
7. Risk / SEO 점수 표시
8. Admin에서 article preview
9. Static HTML publish
10. 중복 기사 방지 DB 저장

### ⏳ MVP 이후로 미룰 것

- 댓글 반응 3–5개 추출 (v1.5)
- Google Trends 연동
- Christian news sites RSS 수집
- 예약 발행
- Whisper background job
- Multi-source trending collector
- CMS 수준의 index 관리

---

## 기사 형식 — News Article (Christian Post 스타일)

### 기사 구조 템플릿

```
Headline:
Pastor [Name] says [central claim] in sermon on [Bible theme]

Subheadline / Deck:
[One strong quote or one-sentence summary]

By [Writer Name or Site Staff]
[Date]

Primary Scripture: [Book Chapter:Verse]

Image Caption:
[Pastor Name] preaching during [sermon/event], [date/source].

Lead:
[Pastor name], [church/ministry], emphasized [main theme] in a recent
sermon on [Scripture], urging believers to [application].

Context / Background:
The message, titled "[Sermon Title]," focused on [topic] and came as part
of [series/event/church service]. The sermon addressed [spiritual / social
/ theological issue].

Scripture Explanation:
Anchoring his message in [Scripture], [Pastor] explained that [biblical point].

Main Message:
[Pastor] argued/emphasized/warned/encouraged that [core claim].

Quote 1:
"[Strong quote from transcript]," he said.

Development:
He connected the passage to [biblical story/doctrine/practical application],
saying that believers should [application].

Quote 2:
"[Second quote]," he added.

Application:
The pastor challenged Christians to [specific action], especially in light of
[modern issue/church life/personal faith].

Conclusion:
The sermon concluded with a call to [faith, repentance, worship, mission, hope],
reminding listeners that [final theological point].

Related Context:
[Optional: previous sermon, church background, ministry context, or related news]

Source:
[YouTube video title + link]
Pastor: [Name], [Church/Ministry]
```

※ 댓글 반응 섹션 (relative한 댓글 3–5개) — MVP 이후 처리.

---

## 기사 형식 — Devotional Blog

### 블로그 구조 템플릿

```
Title:
[Application-centered blog title]
예: "When Life Feels Impossible, Remember That God Is All-Powerful"

Subtitle:
[A short sentence summarizing the spiritual lesson]

Today's Scripture:
[Primary Bible verse, ESV]

Introduction:
[Start with a relatable human struggle, question, or spiritual concern]

Sermon Summary:
[Briefly introduce the pastor, sermon title, and main point]

Biblical Reflection:
[Explain the main Scripture passage in simple and devotional language]

Key Insight:
[Highlight the main theological truth from the sermon]

Quote from the Sermon:
[Insert 1–3 strong quotes from the transcript]

Life Application:
[Explain how readers can apply this truth in daily life]

Reflection Questions:
[3–5 questions for personal meditation]

Prayer:
[A short closing prayer]

Conclusion:
[One final encouraging sentence]

Source:
[YouTube video title + link]
Pastor: [Name], [Church/Ministry]
```

### 기사 vs 블로그 비교

| 구분 | 뉴스 기사 | 블로그 |
|---|---|---|
| 목적 | 보도 | 묵상과 적용 |
| 톤 | 객관적, 기자식 | 따뜻함, 개인적, 적용 중심 |
| 시작 | 누가 무엇을 말했는가 | 독자의 삶과 고민 |
| 성경 사용 | 설교 본문 정보 | 묵상 중심 |
| 인용문 | 3–5개 가능 | 1–3개 정도 |
| 마지막 | 사건/메시지 정리 | 적용, 질문, 기도 |
| 독자 반응 | 정보를 얻음 | 말씀을 묵상함 |

### 버튼 구분

- `Generate News Article`
- `Generate Devotional Blog`

---

## 개발 시작 전 확정이 필요한 5가지

1. MVP 범위 명확화 ✅ (위에 정리됨)
2. DB schema 확정 ✅ (위에 정리됨)
3. API endpoint 목록 작성
4. Transcript timeout / parallel limit 명시 ✅ (위에 정리됨)
5. Article / Risk / SEO JSON output format 확정 ✅ (위에 정리됨)
