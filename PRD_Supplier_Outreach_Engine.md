# PRD: Agentic Supplier Outreach Engine (SOE) v2

## 1. Overview

### Problem
We have a master database of manufacturers across Asia — but a static database doesn't close deals. As we expand beyond luxury towels into new product categories, we need a **modular system** where each product type has its own outreach flow, while a shared supplier database serves as the single source of truth.

### Product
**Supplier Outreach Engine (SOE)** — a modular agentic platform that:
1. **Spins up outreach flows** for any product category on demand
2. **Indexes** manufacturers continuously (discover, qualify, enrich)
3. **Reaches out** via localized, personalized multi-channel sequences
4. **Engages** in real-time conversation with translation support
5. **Hands off** warm leads to human sourcing managers

Each **Flow** is a self-contained outreach pipeline for a specific product category (e.g., "Luxury Towels", "Premium Bedding", "Organic Cotton Apparel"). Flows share the master supplier DB but have their own:
- Qualification criteria (certs, MOQ, capacity thresholds)
- Outreach templates (language, tone, channel sequence)
- Scoring weights (what makes a supplier "high quality" for THIS category)
- Conversation prompts and qualification questions

---

## 2. Goals & Success Metrics

| Goal | Metric | Target |
|------|--------|--------|
| Enrich supplier data | Profile completeness per supplier | ≥90% of fields filled |
| Spin up new flows | Time to launch a new category flow | ≤2 hours |
| Initiate contact | First message sent within 48h of indexing | 100% |
| Get a response | Reply rate from cold outreach | ≥15% |
| Qualify interest | Supplier confirms capability + interest | ≥8% conversion |
| Reduce manual effort | Human touchpoints before handoff | ≤2 per supplier |
| Cross-category reuse | Suppliers reused from previous flows | ≥20% overlap |

---

## 3. Personas

| Persona | Role |
|---------|------|
| **Sourcing Manager** | Selects product categories, reviews qualified leads, takes over warm conversations, makes purchasing decisions |
| **SOE Agent** | Autonomous agent that indexes, reaches out, and qualifies — runs 24/7 per active flow |
| **Supplier Contact** | Manufacturing rep receiving outreach — speaks Chinese, Vietnamese, Japanese, or English |
| **Flow Admin** | Configures new product categories, sets qualification criteria, customizes outreach templates |

---

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│               Supplier Outreach Engine (SOE)                  │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              Flow Dashboard (UI)                         │  │
│  │  Select Category → View Pipeline → Inspect Conversations │  │
│  │  Trigger Agent Actions → Review Qualified Leads           │  │
│  └─────────────────────────┬────────────────────────────────┘  │
│                            │                                    │
│  ┌──────────┬──────────┬──┴──┬──────────┬──────────┐          │
│  │  Towel   │  Bedding │ ... │  Apparel │  NEW +   │          │
│  │  Flow    │  Flow    │     │  Flow    │  Create  │          │
│  │          │          │     │          │          │          │
│  │ INDEX    │ INDEX    │     │ INDEX    │ Template │          │
│  │ OUTREACH│ OUTREACH │     │ OUTREACH │ Picker   │          │
│  │ ENGAGE   │ ENGAGE   │     │ ENGAGE   │          │          │
│  │ HANDOFF  │ HANDOFF  │     │ HANDOFF  │          │          │
│  └────┬─────┴────┬─────┴──┬──┴────┬─────┴──────────┘          │
│       │          │        │       │                            │
│       ▼          ▼        ▼       ▼                            │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │            Master Supplier Database                       │  │
│  │  38+ suppliers · 31+ fields · conversation logs          │  │
│  │  Shared across all flows · single source of truth         │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │           Flow Template Library                           │  │
│  │  Region sequences · Message templates · Qualification     │  │
│  │  criteria · Scoring weights · Channel strategy maps       │  │
│  └─────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

---

## 5. Flow System — Modular Outreach Pipelines

### 5.1 Flow Structure

Each flow is a self-contained outreach pipeline bound to a product category:

```yaml
flow:
  id: "luxury-towels-v1"
  name: "Luxury Towels — Hotel & Resort"
  category: "towels"
  status: "active"  # active | paused | completed
  
  # Which suppliers to target
  targeting:
    regions: ["CN", "TW", "JP", "VN", "TH", "ID"]
    min_quality_score: 40
    required_certs: ["OEKO-TEX"]  # at least one required
    preferred_certs: ["GOTS", "WRAP", "BSCI"]
    min_capacity: "500,000 units/year"
    moq_max: "5,000 units"
    
  # How to score suppliers FOR THIS FLOW
  scoring_weights:
    certifications: 25
    export_market_alignment: 20  # hotel/luxury experience
    production_capacity: 15
    years_in_business: 15
    communication_responsiveness: 15
    financial_stability: 10
    
  # How to reach out
  channel_strategy:
    CN: { primary: "wechat", secondary: "email", tertiary: "alibaba_msg" }
    TW: { primary: "email", secondary: "wechat" }
    JP: { primary: "email_jp", secondary: "phone" }
    VN: { primary: "zalo", secondary: "wechat" }
    ID: { primary: "whatsapp", secondary: "email" }
    TH: { primary: "line", secondary: "email" }
    
  # Outreach sequence templates
  sequences:
    wechat_first:
      steps:
        - day: 0, action: "add_contact", template: "wechat_intro_cn"
        - day: 1, action: "send_message", template: "wechat_first_msg_cn"
        - day: 3, action: "follow_up", template: "wechat_followup_cn"
        - day: 7, action: "final_follow_up", template: "wechat_final_cn"
        - day: 14, action: "mark_unresponsive" if no_reply
    email_first:
      steps:
        - day: 0, action: "send_email", template: "email_intro_en"
        - day: 3, action: "follow_up_email", template: "email_followup_en"
        - day: 7, action: "secondary_channel"
        - day: 14, action: "final_email", template: "email_final_en"
        
  # Qualification questions (asked during ENGAGE)
  qualification_questions:
    - "Can you produce 700-800gsm Egyptian cotton blend towels?"
    - "What is your MOQ for hotel-grade products?"
    - "Can you provide custom jacquard hotel logos?"
    - "Do you hold OEKO-TEX certification? Please share cert number."
    - "Are you willing to sign an NDA before sharing specs?"
    - "What is your sample lead time and cost?"
    
  # Auto-qualification rules
  auto_qualify:
    nda_signed: true
    sample_available: true
    response_within: "5 business days"
    min_quality_score: 60
```

### 5.2 Creating a New Flow

From the dashboard, a Flow Admin clicks **"+ New Flow"** and is guided through:

1. **Category Selection** — Pick from template library or create custom
2. **Targeting Criteria** — Regions, required certs, capacity thresholds, MOQ limits
3. **Channel Strategy** — Auto-suggested based on region, customizable
4. **Message Templates** — Pre-filled from library, editable per region/language
5. **Qualification Questions** — Suggested from similar flows, customizable
6. **Launch** — Agent begins scanning supplier DB for matches, enriches gaps, starts outreach

**Template Library** includes pre-built flows for:
- Luxury Towels (hotel & resort)
- Premium Bedding (sheet sets, duvet covers)
- Organic Cotton Apparel (fashion, basics)
- Kitchen Textiles (chef aprons, oven mitts)
- Bath Accessories (robes, slippers, mats)
- Custom OEM/ODM (any textile product)

### 5.3 Flow Lifecycle

| State | Description |
|-------|-------------|
| **Draft** | Being configured, not yet running |
| **Active** | Agent is indexing + outreaching + engaging |
| **Paused** | Temporarily stopped (e.g., seasonal pause) |
| **Completed** | Enough suppliers qualified, flow archived |
| **Archived** | Historical data preserved, no new outreach |

---

## 6. Phase 1 — INDEX (Discovery & Enrichment)

### 6.1 Continuous Discovery

The agent periodically scans sources to find **new** manufacturers not yet in the database.

**Sources (priority order):**
- Alibaba / Made-in-China / GlobalSources — company directories
- Trade show exhibitor lists (Canton Fair, Intertextile Shanghai, ITMA)
- Industry association member lists
- Competitor supply chain data (public sourcing disclosures)
- Patent/filing databases
- WeChat official account discovery

**Deduplication:**
- Match on: company name (fuzzy), registration number, website domain, phone
- 80%+ match on any two fields → flag as duplicate for human review

### 6.2 Profile Enrichment

For each discovered supplier, the agent fills all fields in the master schema plus flow-specific enrichment.

**Enrichment sources:**
- Company website (contact, products, cert claims)
- B2B platform profiles (MOQ, catalog, trade assurance)
- Government registries (registration, export license)
- Certification databases (OEKO-TEX, GOTS, WRAP, BSCI/SEDEX)
- News/archive (litigation, sanctions, ESG)

### 6.3 Verification & Scoring

**Data Completeness Score** (0–100): weighted sum of filled fields
**Supplier Quality Score** (0–100): flow-specific weights (see §5.1)

Suppliers below flow's minimum quality score are deprioritized.

---

## 7. Phase 2 — OUTREACH (First Contact)

### 7.1 Channel Strategy

Per-flow, per-region. See §5.1 for configurable channel maps.

### 7.2 Outreach Sequences

Multi-touch sequences per channel. See §5.1 for configurable sequence templates.

### 7.3 Message Personalization

Each message is personalized using supplier profile data:
- **Company name** and specific certifications
- **Region-specific** language and cultural norms
- **Product-specific** terminology and requirements
- **A/B testing**: 2 variants per first-touch message, track which performs better

### 7.4 Compliance

- Anti-spam: max 1 message per 3 days per channel per supplier
- Disclosure: agent identifies as representing [Brand Name]
- Opt-out: any "not interested" response → immediate halt, flag in DB
- GDPR/PIPA: respect data privacy regulations per region
- WeChat safety: avoid trigger words, rate-limit to prevent account bans

---

## 8. Phase 3 — ENGAGE (Conversation & Qualification)

### 8.1 Conversation Handler

The agent handles inbound replies in real-time:
- **Language detection** → auto-switch conversation language
- **Intent classification** → interested / not interested / need more info / pricing question
- **Translation** → all non-English messages auto-translated for team visibility
- **Conversation state** → tracked per supplier per flow

### 8.2 Qualification Flow

When supplier shows interest, agent enters qualification mode:
1. Ask qualification questions (from flow config §5.1)
2. Record answers in supplier profile
3. Score qualification completeness
4. If all critical questions answered → mark **QUALIFIED**
5. If any critical question unanswered → follow up within 48h

### 8.3 Escalation Rules

The agent **always escalates** to a human when:
- Pricing negotiation begins
- Contract terms discussed
- Supplier asks to speak with a manager
- Ambiguous or culturally sensitive response
- Supplier expresses concern about automated communication
- Any legal/compliance topic raised

---

## 9. Phase 4 — HANDOFF (Human Takeover)

### 9.1 Handoff Brief

When a supplier is qualified, the agent generates a **Handoff Brief**:

```
HANDOFF BRIEF — [Supplier Name]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Flow: Luxury Towels v1
State: QUALIFIED
Quality Score: 78/100
Completeness: 85/100

Profile Summary:
  - OEKO-TEX verified (cert #25.HCN.71832)
  - MOQ: 500 units/style
  - 30+ years hotel textile experience
  - Custom jacquard capability confirmed

Conversation Highlights:
  - Responded within 24h to initial outreach
  - Confirmed all qualification criteria
  - NDA signed
  - Sample pricing: $19/unit including shipping
  - Eager to schedule video call

Recommended Next Steps:
  1. Schedule video call with production team
  2. Order samples (3 sizes, 5 units each)
  3. Discuss volume pricing for 100K+ units
  4. Request production facility photos/videos

Escalation Reason: Supplier requested direct manager contact
```

### 9.2 Notification Channels

- Telegram message to Sourcing Manager with brief summary
- Dashboard notification badge on qualified card
- Email digest (daily summary of all qualified suppliers)

---

## 10. Master Supplier Database

### 10.1 Schema

The master DB is shared across ALL flows. Each supplier has:

**Core fields** (universal across all categories):
- Company info: legal name, trade name, type, country, HQ city, factory locations
- Contact: contact name, email, phone, WeChat, website, LinkedIn
- Credentials: certifications, audits, years in business, annual revenue
- Production: capacity, MOQ, lead time, product categories, export markets

**Flow-specific fields** (per-flow overlay):
- outreach_state (per flow)
- qualification_score (per flow)
- conversation_log (per flow)
- qualification_answers (per flow)

A supplier can be in multiple flows simultaneously (e.g., qualified for Towels, conversing for Bedding).

### 10.2 Single Source of Truth

- One supplier record → many flow states
- Enrichment done once → benefits all active flows
- Conversation history preserved → searchable across flows
- Duplicate detection → global, not per-flow

---

## 11. Dashboard UI

### 11.1 Flow Selector

Top-level navigation: dropdown or sidebar listing all active flows.
- Click a flow → see its pipeline, funnel, conversations
- Click "+ New Flow" → guided flow creation wizard

### 11.2 Pipeline View (per flow)

Kanban columns: Discovered → Enriched → Outreach Sent → Conversing → Qualified → Human Handoff
+ Not Interested | Unresponsive (sidebar columns)

### 11.3 Conversation Viewer (per supplier per flow)

Chat-style timeline with:
- Agent messages (🤖) vs Supplier messages (👤)
- Channel badges (WeChat, Email, Zalo, LINE, WhatsApp)
- Agent action tags (⚡ DRAFT, NDA_SENT, QUALIFY, etc.)
- 🌐 Translation toggle for non-English messages
- Suggested reply chips based on current state
- Compose message bar

### 11.4 Flowchart View

Interactive flowchart showing the full agentic workflow:
- Click any phase node → see detailed steps
- Click any transition → see rules and triggers

### 11.5 Funnel View

Conversion funnel per flow showing drop-off rates at each stage.

---

## 12. Technical Stack

| Component | Technology |
|-----------|-----------|
| Master DB | SQLite (local) → PostgreSQL (scale) |
| Flow Config | YAML files per flow |
| Agent Runtime | Hermes Agent (cron + delegate_task) |
| Conversation Store | SQLite conversation_log table |
| Message Channels | WeChat Official Account API, SMTP (email), Zalo API, LINE API, WhatsApp Business API |
| Translation | faster-whisper (STT) + built-in translation layer |
| Dashboard | Self-contained HTML + baked JSON → Cloudflare Pages |
| Deployment | Wrangler → Cloudflare Pages |

---

## 13. Milestones

| Phase | Deliverable | Timeline |
|-------|------------|----------|
| **M0** | Master DB + single flow (Luxury Towels) + dashboard | ✅ Done |
| **M1** | Flow template library + "New Flow" wizard UI | Week 1 |
| **M2** | Multi-flow DB schema (supplier ↔ flow_state mapping) | Week 1 |
| **M3** | WeChat Official Account integration (real messaging) | Week 2-3 |
| **M4** | Agent runtime: cron-based outreach + conversation handler | Week 2-3 |
| **M5** | Translation layer (auto-detect + translate in conversation view) | Week 2 |
| **M6** | Email channel integration (SMTP via himalaya or API) | Week 3 |
| **M7** | Qualification scoring + auto-qualify logic | Week 3 |
| **M8** | Handoff brief generation + Telegram notification | Week 4 |
| **M9** | A/B testing framework for outreach messages | Week 4 |
| **M10** | Full production deployment + monitoring | Week 5 |

---

## 14. Open Questions

| # | Question | Impact | Status |
|---|----------|--------|--------|
| 1 | WeChat Official Account registration — who sets this up? | Blocks CN outreach | Open |
| 2 | Brand identity — what company name does the agent use? | Blocks all outreach | Open |
| 3 | Pricing parameters — acceptable MOQ, price per unit ranges? | Blocks qualification | Open |
| 4 | NDA template — legal review needed? | Blocks ENGAGE phase | Open |
| 5 | Zalo/LINE/WhatsApp Business API — accounts needed? | Blocks VN/TH/ID outreach | Open |
| 6 | Sourcing Manager assignment — who receives handoffs? | Blocks HANDOFF phase | Open |
| 7 | Budget for paid search/API access (Alibaba, cert databases)? | Blocks INDEX phase | Open |
| 8 | Product category priority — what's the next flow after towels? | Blocks M1 milestone | Open |