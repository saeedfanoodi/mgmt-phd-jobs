# Management PhD Faculty Jobs Tracker

Static tracker for tenure-track Management / Strategy / AI / Entrepreneurship faculty positions across USA, Canada, Australia, NZ, UAE. Modeled after mgmtphdjobs.com.

## Update flow
Daily scheduled task regenerates `index.html` with new postings and commits to `main`. GitHub Pages workflow deploys automatically.

## Manual deploy
```
git add index.html && git commit -m "Update jobs $(date +%F)" && git push
```
