"""Prompt templates for the two-pass AI analysis pipeline."""

SYSTEM_PROMPT = """You are a senior consumer insights analyst. You analyze consumer reviews \
and feedback with precision, extracting actionable insights for product teams and strategists. \
Always ground your analysis in specific evidence from the reviews. Use exact quotes when possible."""

BATCH_ANALYSIS_PROMPT = """Analyze the following {n_reviews} consumer reviews about "{query}" ({query_type}).

For each review, extract:
1. **Sentiment**: positive, negative, neutral, or mixed
2. **Themes**: Short noun-phrase labels (e.g., "Taste/Flavor", "Sleep Quality", "Price Value")
3. **Notable quotes**: Verbatim excerpts (1-3 sentences) worth highlighting
4. **Unmet needs**: Any wishes, complaints, or desires for improvement

Then aggregate across ALL reviews in this batch:
- Theme frequency counts with sentiment breakdown
- Top quotes with full source attribution (review_id, author, source platform)
- Consolidated list of unmet needs

Reviews (JSON):
{reviews_json}"""

SYNTHESIS_PROMPT = """You are synthesizing consumer insights from {total_reviews} reviews about \
"{query}" collected across {n_sources} platforms ({source_list}).

Below are batch-level analysis results. Produce a final unified report:

1. **EXECUTIVE SUMMARY**: 3-5 sentence overview of the most important findings. Lead with the \
single most actionable insight.

2. **THEMES**: Merge and deduplicate themes across batches. Rank by frequency. For each theme, \
provide a description, sentiment breakdown, source distribution, and 2-3 representative quotes.

3. **SENTIMENT**: Overall sentiment label and score (-1.0 to 1.0). Per-source sentiment scores. \
Distribution across positive/negative/neutral/mixed.

4. **UNMET NEEDS**: Consolidate and deduplicate. Score each by opportunity (0.0 to 1.0) based on \
frequency and intensity of consumer desire. Include supporting quotes.

5. **PERSONAS**: Generate 3-5 distinct consumer personas from the review patterns. Each persona \
needs a memorable name, description, demographics hints, motivations, pain points, representative \
quotes, and estimated prevalence (e.g., "~25% of reviewers").

6. **KEY QUOTES**: Select the 10-15 most insightful, memorable, or representative quotes across \
all batches. Ensure diversity of source, sentiment, and theme.

Batch results (JSON):
{batch_results_json}"""
