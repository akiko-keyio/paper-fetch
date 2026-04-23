# Paper Fetch

下载学术论文 PDF 并转换为 Markdown 的 Agent Skill。支持通过 DOI、论文标题或 URL 获取论文。

## How to Use

在安装此 Skill 后，可以直接在 AGENT 对话中使用：

> "下载 xxx 论文，并转换成 Markdown"

> "下载 xxx 论文 PDF，不需要转换"

> "把文件夹 xxx 中的 PDF 都转成 Markdown"

## Note

- PDF 转 Markdown 依赖 [Datalab](https://www.datalab.to) 的 API。注册账号后会赠送 $5 额度（约可转换 1250 页），在 [API Keys](https://www.datalab.to/app/keys) 页面生成密钥，填入 `.env` 文件即可。没有密钥时会自动退回到 pypdf 做基础文本提取。
- 下载付费论文前，agent 会打开浏览器窗口，并提示你完成你所在机构的 SSO 登录，之后 cookie 会被保存下来供后续使用。
