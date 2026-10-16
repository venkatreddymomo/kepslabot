<%@ Page Language="C#" AutoEventWireup="true" %>
<!doctype html>
<html lang="en">
<head runat="server">
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Zone — Embed Test (ASP.NET)</title>
    <style>
        body { font-family: Segoe UI, system-ui, sans-serif; margin: 0; padding: 40px; background: #f1f5f9; color: #0f172a; }
        .card { max-width: 760px; margin: 0 auto; background: #fff; padding: 28px 32px; border-radius: 14px; box-shadow: 0 10px 30px rgba(15,23,42,.08); }
        h1 { margin: 0 0 8px; font-size: 22px; }
        p { line-height: 1.6; color: #475569; }
        code { background:#f1f5f9; padding:2px 6px; border-radius:4px; font-size:13px; }
    </style>
</head>
<body>
    <form id="form1" runat="server">
        <div class="card">
            <h1>Zone — widget embed test page</h1>
            <p>This is a plain ASP.NET WebForms page. The only thing that matters is the
               single <code>&lt;script&gt;</code> tag at the bottom of the body — copy that block
               onto any page of the live .NET site and the launcher appears in the corner.</p>
            <p>Test checklist: send a message, reload the page — the chat, the images, the
               <em>Book on website</em> button and the <em>Get help from our team</em> button
               below it must all still be there; open a form, reload mid-fill, and the form
               must come back.</p>
        </div>
    </form>

    <!-- ====== KEPSLA CHAT WIDGET — copy this block onto the live site ====== -->
    <script src="http://localhost:8011/static/embed/kebo-widget-loader.js"
            data-widget-key="wk_kOKo8Vwt1rGBE7ZCSLhYaFeC"
            async></script>
    <!-- ==================================================================== -->
</body>
</html>
