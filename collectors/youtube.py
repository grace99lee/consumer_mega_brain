from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from googleapiclient.discovery import build

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class YouTubeCollector(BaseCollector):
    source_type = SourceType.YOUTUBE

    def __init__(self, settings: Settings):
        super().__init__(settings)
        if self.is_available():
            self._youtube = build("youtube", "v3", developerKey=settings.youtube_api_key)

    def is_available(self) -> bool:
        return self.settings.has_youtube()

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        if not self.is_available():
            logger.warning("YouTube collector not available — missing API key")
            return []
        return await asyncio.to_thread(self._collect_sync, query, max_results)

    def _collect_sync(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        try:
            # Search for relevant videos
            search_resp = self._youtube.search().list(
                q=query,
                part="snippet",
                type="video",
                maxResults=min(max_results // 10, 25),
                order="relevance",
            ).execute()

            video_ids = [
                item["id"]["videoId"]
                for item in search_resp.get("items", [])
                if item.get("id", {}).get("videoId")
            ]

            for video_id in video_ids:
                if len(reviews) >= max_results:
                    break

                # Get video details
                video_resp = self._youtube.videos().list(
                    part="snippet,statistics",
                    id=video_id,
                ).execute()

                video_info = video_resp["items"][0] if video_resp.get("items") else None

                # Fetch comment threads
                try:
                    comments = self._fetch_comments(video_id, max_per_video=max_results // len(video_ids))
                    for comment in comments:
                        review = self._comment_to_review(comment, video_id, video_info)
                        reviews.append(review)
                except Exception:
                    logger.warning("Comments disabled or error for video %s", video_id)

        except Exception:
            logger.exception("Error collecting YouTube data for query: %s", query)

        logger.info("Collected %d reviews from YouTube", len(reviews))
        return reviews[:max_results]

    def _fetch_comments(self, video_id: str, max_per_video: int = 50) -> list[dict]:
        comments = []
        next_page = None

        while len(comments) < max_per_video:
            resp = self._youtube.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=min(100, max_per_video - len(comments)),
                order="relevance",
                pageToken=next_page,
            ).execute()

            for item in resp.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                comments.append(snippet)

            next_page = resp.get("nextPageToken")
            if not next_page:
                break

        return comments

    def _comment_to_review(self, comment: dict, video_id: str, video_info: dict | None) -> Review:
        published = comment.get("publishedAt", "")
        date = None
        if published:
            try:
                date = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                pass

        video_title = ""
        if video_info:
            video_title = video_info["snippet"].get("title", "")

        return Review(
            id=f"youtube_{video_id}_{comment.get('authorDisplayName', 'anon')[:20]}_{published[:10] if published else 'nodate'}",
            source=SourceType.YOUTUBE,
            author=comment.get("authorDisplayName"),
            text=comment.get("textDisplay", ""),
            rating=None,
            date=date,
            url=f"https://www.youtube.com/watch?v={video_id}",
            product_name=None,
            metadata={
                "video_id": video_id,
                "video_title": video_title,
                "like_count": comment.get("likeCount", 0),
                "type": "comment",
            },
        )
