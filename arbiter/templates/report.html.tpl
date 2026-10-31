<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>审计报告 · @@model@@</title>
<style>
  :root {
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #1c1f23;
    --muted: #6b7280;
    --line: #e5e7eb;
    --accent: #2f6feb;
    --ok: #1a7f4b;
    --warn: #a16207;
    --bad: #b42318;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 32px 20px 64px;
    background: var(--bg);
    color: var(--ink);
    font: 15px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  }
  .wrap { max-width: 980px; margin: 0 auto; }
  header.top { margin-bottom: 24px; }
  h1 { font-size: 20px; margin: 0 0 6px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 13px; word-break: break-all; }
  .score-box {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 24px 28px;
    display: flex;
    align-items: baseline;
    gap: 18px;
    flex-wrap: wrap;
    margin-bottom: 18px;
  }
  .score-num { font-size: 52px; font-weight: 700; line-height: 1; }
  .score-unit { font-size: 16px; color: var(--muted); }
  .score-side { font-size: 13px; color: var(--muted); flex: 1 1 260px; }
  .score-side b { color: var(--ink); }
  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px 20px;
    margin-bottom: 14px;
  }
  .card header { display: flex; align-items: center; gap: 10px; }
  .card h2 { font-size: 15px; margin: 0; font-weight: 600; }
  .badge {
    font-size: 12px;
    padding: 2px 9px;
    border-radius: 999px;
    background: #eef1f5;
    color: var(--muted);
  }
  .badge-pass, .badge-match, .badge-clean, .badge-measured, .badge-estimated {
    background: #e6f4ec; color: var(--ok);
  }
  .badge-injected, .badge-fail, .badge-mismatch, .badge-error {
    background: #fdecea; color: var(--bad);
  }
  .badge-suspect, .badge-unreported, .badge-unreliable, .badge-family, .badge-partial {
    background: #fdf3e0; color: var(--warn);
  }
  .summary { margin: 10px 0 4px; }
  .meta { margin: 0; font-size: 13px; color: var(--muted); }
  .pts { font-weight: 600; color: var(--ink); }
  .coef { margin-left: 10px; }
  .reason { margin: 8px 0 0; font-size: 13px; color: var(--muted); }
  details { margin-top: 10px; }
  summary { cursor: pointer; font-size: 13px; color: var(--accent); }
  details ul { margin: 8px 0 0; padding-left: 20px; font-size: 13px; color: var(--muted); }
  details li { margin-bottom: 3px; word-break: break-all; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; }
  .skipped { font-size: 13px; color: var(--warn); margin-bottom: 16px; }
  .skipped ul { margin: 6px 0 0; padding-left: 20px; }
  footer {
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    font-size: 12px;
    color: var(--muted);
  }
</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <h1>网关审计报告 · @@model@@</h1>
    <div class="sub">
      网关 @@host@@ ｜ 协议 @@protocol@@ ｜ 评分版本 @@scoring_version@@<br>
      job @@id@@ ｜ 生成于 @@created_at@@
    </div>
  </header>

  <div class="score-box">
    <div><span class="score-num">@@score@@</span><span class="score-unit"> / 100</span></div>
    <div class="score-side">
      <b>证据完整度</b> @@integrity_score@@ （@@integrity_status@@，覆盖 @@integrity_coverage@@%）<br>
      @@total_reason@@
    </div>
  </div>

  @@skipped@@

  @@cards@@

  <section class="card">
    <header><h2>探测记录</h2></header>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>状态</th><th>耗时</th><th>错误</th></tr></thead>
      <tbody>@@probe_rows@@</tbody>
    </table>
  </section>

  <footer>
    本报告由 Arbiter 生成（复刻研究项目）。<br>
    仅供对自有网关做合规自检使用；密钥不出本机，请自行承担使用风险。
  </footer>

</div>
</body>
</html>
