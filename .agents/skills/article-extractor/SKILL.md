---
name: article-extractor
description: MQL5.com article extractor (manual load only)
allowed-tools: Read, Bash, Grep, Glob, WebFetch
autoActivate: false
---

# MQL5 Article Extractor

**Loaded via:** `/sky_tower skill articles`

Extract and organize technical articles from mql5.com for trading research.

## This Skill Covers

- Article extraction from mql5.com ONLY
- User profile article discovery
- Batch URL processing
- Documentation organization

## Scope Restriction

**IMPORTANT:** This skill works ONLY with mql5.com domain.

For other financial sites (Reuters, Bloomberg, Yahoo Finance, etc.), respond:
> "This skill is designed exclusively for mql5.com content. For other sources, please use general web research tools."

## Input Methods

### 1. Direct URL
```
User: Extract this article: https://www.mql5.com/en/articles/12345
```

### 2. User Profile
```
User: Get all articles by user terrylica
User: Get articles from user ID 12345
```

### 3. Batch File
```
User: Process URLs from articles_list.txt
```

### 4. Topic Search
```
User: Find articles about RSI indicator
User: Search for Python MT5 integration
```

## Article Categories

### MetaTrader 5 Development
- Custom indicators
- Expert Advisors
- Scripts and utilities
- Trading panels

### Python Integration
- MetaTrader5 Python package
- Data export/import
- Machine learning integration
- Automated testing

### Trading Strategies
- Technical analysis
- Algorithmic trading
- Risk management
- Portfolio optimization

### API Documentation
- Trade functions
- Market info functions
- Chart operations
- Event handling

## Extraction Workflow

### Step 1: Validate URL
```python
# Must be mql5.com domain
if "mql5.com" not in url:
    return "Only mql5.com URLs supported"
```

### Step 2: Fetch Content
```bash
# Use WebFetch for article content
WebFetch(url, "Extract article title, author, date, and full content")
```

### Step 3: Parse Structure
- Title
- Author
- Publication date
- Article body
- Code snippets
- Images/diagrams (note their presence)

### Step 4: Save Output
```
articles/
├── {date}_{title_slug}.md
├── code/
│   └── {filename}.mq5
└── index.md
```

## Output Format

### Markdown Article
```markdown
# Article Title

**Author:** username
**Date:** 2026-01-20
**Source:** https://www.mql5.com/en/articles/12345

## Summary
Brief overview of article content...

## Key Points
- Point 1
- Point 2

## Code Snippets

### indicator.mq5
```mql5
// Code here
```

## References
- Link 1
- Link 2
```

## Useful MQL5.com URLs

### Documentation
- https://www.mql5.com/en/docs - Official documentation
- https://www.mql5.com/en/docs/trading - Trading functions
- https://www.mql5.com/en/docs/indicators - Indicator functions

### Articles
- https://www.mql5.com/en/articles - All articles
- https://www.mql5.com/en/articles/python - Python articles
- https://www.mql5.com/en/articles/expert - EA articles

### Code Base
- https://www.mql5.com/en/code - Free code library
- https://www.mql5.com/en/code/experts - Expert Advisors
- https://www.mql5.com/en/code/indicators - Indicators

## SkyTower-AI Context

Relevant article topics for this project:
- News trading strategies
- Economic calendar integration
- WebRequest for API calls
- Risk management in EAs
- Python MT5 data export

Project location: `C:\Users\pietr\Documents\Sky tower\SkyTowerAI\`
