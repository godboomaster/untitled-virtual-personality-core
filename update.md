Rich messages
The following methods and objects allow your bot to handle and send rich messages.

Rich Message Formatting Options
Rich messages support advanced structured formatting options like headings, lists, tables, media, block quotations, collapsible blocks, footnotes, and formulas. Telegram clients will render them accordingly. You can specify rich message content using Markdown-style or HTML-style formatting.

Plain URLs, e-mail addresses, username mentions, hashtags, cashtags, bot commands, phone numbers, and bank card numbers are detected automatically. To disable automatic entity detection, pass True in the skip_entity_detection field. Note that Telegram clients will display an alert to the user before opening an inline link ('Open this link?' together with the full URL).

Rich Message Limits
Rich messages are subject to the following limits:

Up to 32768 UTF-8 characters in the rich message text, including custom emoji alternative text and formula source.
Up to 500 blocks, including nested blocks, list items, ordered list items, table rows, quotation blocks, and details blocks.
Up to 16 levels of nested formatting and blocks.
Up to 50 media attachments in total, including photos, videos, and audio files.
Up to 20 columns in a table.
Rich Markdown style
To use this mode, pass rich message content in the markdown field. Use the following syntax in your message:

**bold text**
__bold text__
*italic text*
_italic text_
~~strikethrough text~~
`inline fixed-width code`
==marked text==
||spoiler||

[inline URL](https://t.me/)
[inline e-mail](mailto:user@example.com)
[inline phone number](tel:+123456789)
[inline mention of a user](tg://user?id=123456789)
![👍](tg://emoji?id=5368324170671202286)
![22:45 tomorrow](tg://time?unix=1647531900&format=wDT)
$x^2 + y^2$
\#hashtag $USD +12345678901, card: 4242 4242 4242 4242, https://t.me t.me a@t.me /command @username
all the text above was on the same line

# Heading 1
## Heading 2
### Heading 3
#### Heading 4
##### Heading 5
###### Heading 6

Paragraph text

```python
  print('pre-formatted fixed-width code block written in the Python programming language')
```

---

- unordered list item
* unordered list item
+ unordered list item

1. ordered list item
2. ordered list item

- [ ] task list item
- [x] completed task list item

>Block quotation started
>
>Block quotation continued on the next line
>Block quotation continued on the same line
>
>The last line of the block quotation

![](https://telegram.org/example/photo.jpg)
![](https://telegram.org/example/video.mp4)
![](https://telegram.org/example/audio.mp3)
![](https://telegram.org/example/audio.ogg)
![](https://telegram.org/example/animation.gif)

![](https://telegram.org/example/photo.jpg "Photo caption")
![](https://telegram.org/example/video.mp4 "Video caption")
![](https://telegram.org/example/audio.mp3 "Audio caption")
![](https://telegram.org/example/audio.ogg "Voice note caption")
![](https://telegram.org/example/animation.gif "Animation caption")

| Header 1 | Header 2 |
|:---------|:--------:|
| left     | center   |

Text with a reference[^id1] and another one[^id2].

[^id1]: Definition of the first footnote.
[^id2]: Definition of the second footnote.

$$E = mc^2$$

```math
E = mc^2
```

## Example Nested Syntax Report for _Q1_
Intro with <u>underlined text</u>, ==marked text==, and $x^2 + y^2$.
**Bold _italic <u>underlined italic bold</u> italic_ bold**
<u>In inline tags, nested **markdown** is parsed</u>
>Quote with **bold text, ~~strikethrough, and <tg-spoiler>spoiler</tg-spoiler>~~**, plus [a link](https://t.me/).

- List item with `code`, <sup>superscript</sup>, <sub>subscript</sub>, and a footnote[^note]
- Another item with **bold <tg-spoiler><code>spoiler code</code></tg-spoiler>**
- Another item with ~~strikethrough and <ins>inserted text</ins>~~

| Metric | Value |
|:-------|------:|
| Speed  | **42** <sup>ms</sup> |
| Status | <tg-spoiler>ready</tg-spoiler> |

[^note]: Footnote with _italic text_ and <u>HTML underline</u>.

---

# Details blocks can contain Markdown content:

<details open><summary>Summary with **bold text**</summary>

### Details heading
- List item with _italic text_
- List item with <tg-spoiler>spoiler</tg-spoiler>

</details>

# Collages and slideshows can contain Markdown media blocks:

<tg-collage>

![](https://telegram.org/example/photo.jpg)
![](https://telegram.org/example/video.mp4)

</tg-collage>

<tg-slideshow>

![](https://telegram.org/example/photo.jpg)
![](https://telegram.org/example/video.mp4)

</tg-slideshow>
For formatting features that don't have Markdown syntax, use HTML tags:

<u>underlined text</u>, <ins>underlined text</ins>
<sub>subscript text</sub>
<sup>superscript text</sup>
<a name="chapter-1"></a>
<aside>Pull quote<cite>The Author</cite></aside>
<details open><summary>Title</summary>Content</details>
<tg-map lat="41.9" long="12.5" zoom="14"/>
<tg-collage><img src="https://telegram.org/example/photo.jpg"/><figcaption>Caption<cite>The Author</cite></figcaption></tg-collage>
<tg-slideshow><img src="https://telegram.org/example/photo.jpg"/><video src="https://telegram.org/example/video.mp4"/><figcaption>Slideshow caption<cite>The Author</cite></figcaption></tg-slideshow>
Please note:

Rich Markdown is compatible with GitHub Flavored Markdown where possible and can contain arbitrary HTML. Supported rich message HTML tags are parsed as described in Rich HTML style.
Media can be specified only as a separate block.
Media blocks support only HTTP and HTTPS URLs.
Media type is determined by the MIME type and the URL of the media.
In media syntax, the optional title after the URL is used as the caption; for example,  displays “Photo caption” under the media.
Table cells can contain only inline formatting.
Formula source is treated as raw LaTeX.
See date-time entity formatting for more details about supported date-time formats.
Rich HTML style
To use this mode, pass rich message content in the html field. The following tags are currently supported:

<a name="chapter-0"></a>
<b>bold text</b>, <strong>bold text</strong>
<i>italic text</i>, <em>italic text</em>
<u>underlined text</u>, <ins>underlined text</ins>
<s>strikethrough text</s>, <strike>strikethrough text</strike>, <del>strikethrough text</del>
<code>inline fixed-width code</code>
<mark>marked text</mark>
<sub>subscript text</sub>
<sup>superscript text</sup>
<tg-spoiler>spoiler</tg-spoiler>

<a href="#note-1">Reference</a>
<a href="https://t.me/">inline URL</a>
<a href="mailto:user@example.com">inline e-mail</a>
<a href="tel:+123456789">inline phone number</a>
<a href="tg://user?id=123456789">inline mention of a user</a>
<a href="#chapter-1">in-document link</a>
<a name="chapter-1"></a>

<tg-reference name="note-1">Referenced text</tg-reference>
<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>
<img src="tg://emoji?id=5368324170671202286" alt="👍"/>
<tg-time unix="1647531900" format="wDT">22:45 tomorrow</tg-time>
<tg-math>x^2 + y^2</tg-math>

#hashtag $USD +12345678901, card: 4242 4242 4242 4242, https://t.me t.me a@t.me /command @username

all the text above was on the same line

<h1>Heading 1</h1>
<h2>Heading 2</h2>
<h3>Heading 3</h3>
<h4>Heading 4</h4>
<h5>Heading 5</h5>
<h6>Heading 6</h6>

<a name="chapter-2"></a>

<p>Paragraph text</p>
<pre>pre-formatted fixed-width code block</pre>
<pre><code class="language-python">  print('pre-formatted fixed-width code block written in the Python programming language')</code></pre>
<footer>Footer text</footer>
<hr/>
<ul><li>unordered list item</li></ul>
<ol><li>ordered list item</li></ol>
<ol start="3" type="a" reversed><li>ordered list item</li></ol>
<ol><li value="7" type="i">ordered list item with explicit number</li></ol>
<ul>
<li><input type="checkbox" checked>Checked checkbox</li>
<li><input type="checkbox">Unchecked checkbox</li>
</ul>

<blockquote>Block quotation started<br>Block quotation continued<br>The last line of the block quotation<cite>The Author</cite></blockquote>
<aside>Pull quote<cite>The Author</cite></aside>

<img src="https://telegram.org/example/photo.jpg"/>
<video src="https://telegram.org/example/video.mp4"></video>
<audio src="https://telegram.org/example/audio.mp3"></audio>
<audio src="https://telegram.org/example/audio.ogg"></audio>
<video src="https://telegram.org/example/animation.gif"></video>

<figure><img src="https://telegram.org/example/photo.jpg" tg-spoiler/><figcaption>Photo caption<cite>Photo credit</cite></figcaption></figure>
<figure><video src="https://telegram.org/example/video.mp4" tg-spoiler></video><figcaption>Video caption</figcaption></figure>
<figure><audio src="https://telegram.org/example/audio.mp3"></audio><figcaption>Audio caption</figcaption></figure>
<figure><audio src="https://telegram.org/example/audio.ogg"></audio><figcaption>Voice note caption</figcaption></figure>
<figure><video src="https://telegram.org/example/animation.gif" tg-spoiler></video><figcaption>Animation caption</figcaption></figure>

<tg-map lat="41.9" long="12.5" zoom="14"/>
<figure><tg-map lat="41.9" long="12.5" zoom="14"/><figcaption>Map caption</figcaption></figure>

<tg-collage><img src="https://telegram.org/example/photo.jpg"/><video src="https://telegram.org/example/video.mp4"/></tg-collage>
<tg-collage><video src="https://telegram.org/example/video.mp4"/><img src="https://telegram.org/example/photo.jpg"/><figcaption>Collage caption</figcaption></tg-collage>
<tg-slideshow><img src="https://telegram.org/example/photo.jpg"/><video src="https://telegram.org/example/video.mp4"/></tg-slideshow>
<tg-slideshow><video src="https://telegram.org/example/video.mp4"/><img src="https://telegram.org/example/photo.jpg"/><figcaption>Slideshow caption</figcaption></tg-slideshow>

<table><tr><th>Header 1</th><th>Header 2</th></tr><tr><td>Value 1</td><td>Value 2</td></tr></table>
<table bordered striped><caption>Table caption</caption>
<tr><td colspan="2" rowspan="2" align="left">Value</td><td align="center">Value2</td><td align="right">Value3</td></tr>
<tr><td valign="top">Value4</td><td valign="middle">Value5</td><td valign="bottom">Value6</td></tr>
<tr><td>Value7</td></tr></table>

<details><summary>Title</summary>Content</details>
<details open><summary>Title</summary>Content</details>
<tg-math-block>E = mc^2</tg-math-block>
Please note:

Only the tags mentioned above are currently supported.
All numerical HTML entities are supported.
The API currently supports only the following named HTML entities: &lt;, &gt;, &amp;, &quot;, &apos;, &nbsp;, &hellip;, &mdash;, &ndash;, &lsquo;, &rsquo;, &ldquo; and &rdquo;.
Use nested pre and code tags to define the programming language for a pre-formatted block.
Programming language can't be specified for standalone code tags.
Links mailto:..., tel:..., and tg://user?id=... are rendered as e-mail links, phone links, and inline mentions respectively. Other supported links are rendered as regular inline links.
Images, videos, and audio files can be specified only as separate media blocks.
Media blocks support only HTTP and HTTPS URLs.
An empty <a name="..."></a> on its own creates an anchor that can be linked to with <a href="#...">...</a>.
In <figcaption>, you can use <cite> tags to specify caption credit.
Use <tg-reference name="...">...</tg-reference> to define referenced text that can be linked to with <a href="#...">...</a>.
The body of a <details> tag can contain rich message content. If the open attribute is specified, the block is expanded by default.
Formula source is treated as raw LaTeX.
See date-time entity formatting for more details about supported date-time formats.
RichMessage
Rich formatted message.

Field	Type	Description
blocks	Array of RichBlock	Content of the message
is_rtl	Boolean	Optional. True, if the rich message must be shown right-to-left
InputRichMessage
Describes a rich message to be sent. Exactly one of the fields html or markdown must be used.

Field	Type	Description
html	String	Optional. Content of the rich message to send described using HTML formatting. See rich message formatting options for more details.
markdown	String	Optional. Content of the rich message to send described using Markdown formatting. See rich message formatting options for more details.
is_rtl	Boolean	Optional. Pass True if the rich message must be shown right-to-left
skip_entity_detection	Boolean	Optional. Pass True to skip automatic detection of entities (e.g., URLs, email addresses, username mentions, hashtags, cashtags, bot commands, or phone numbers) in the text
sendRichMessage
Use this method to send rich messages. If the message contains a block with a media element, then the bot must have the right to send the media to the chat. On success, the sent Message is returned.

Parameter	Type	Required	Description
business_connection_id	String	Optional	Unique identifier of the business connection on behalf of which the message will be sent
chat_id	Integer or String	Yes	Unique identifier for the target chat or username of the target bot, supergroup or channel in the format @username
message_thread_id	Integer	Optional	Unique identifier for the target message thread (topic) of a forum; for forum supergroups and private chats of bots with forum topic mode enabled only
direct_messages_topic_id	Integer	Optional	Identifier of the direct messages topic to which the message will be sent; required if the message is sent to a direct messages chat
rich_message	InputRichMessage	Yes	The message to be sent
disable_notification	Boolean	Optional	Sends the message silently. Users will receive a notification with no sound.
protect_content	Boolean	Optional	Protects the contents of the sent message from forwarding and saving
allow_paid_broadcast	Boolean	Optional	Pass True to allow up to 1000 messages per second, ignoring broadcasting limits for a fee of 0.1 Telegram Stars per message. The relevant Stars will be withdrawn from the bot's balance.
message_effect_id	String	Optional	Unique identifier of the message effect to be added to the message; for private chats only
suggested_post_parameters	SuggestedPostParameters	Optional	A JSON-serialized object containing the parameters of the suggested post to send; for direct messages chats only. If the message is sent as a reply to another suggested post, then that suggested post is automatically declined.
reply_parameters	ReplyParameters	Optional	Description of the message to reply to
reply_markup	InlineKeyboardMarkup or ReplyKeyboardMarkup or ReplyKeyboardRemove or ForceReply	Optional	Additional interface options. A JSON-serialized object for an inline keyboard, custom reply keyboard, instructions to remove a reply keyboard or to force a reply from the user.
sendRichMessageDraft
Use this method to stream a partial rich message to a user while the message is being generated. Note that the streamed draft is ephemeral and acts as a temporary 30-second preview - once the output is finalized, you must call sendRichMessage with the complete message to persist it in the user's chat. Returns True on success.

Parameter	Type	Required	Description
chat_id	Integer	Yes	Unique identifier for the target private chat
message_thread_id	Integer	Optional	Unique identifier for the target message thread
draft_id	Integer	Yes	Unique identifier of the message draft; must be non-zero. Changes to drafts with the same identifier are animated.
rich_message	InputRichMessage	Yes	The partial message to be streamed
RichText
This object represents a rich formatted text. Currently, it can be either a String for plain text, an Array of RichText, or any of the following types:

RichTextBold
RichTextItalic
RichTextUnderline
RichTextStrikethrough
RichTextSpoiler
RichTextDateTime
RichTextTextMention
RichTextSubscript
RichTextSuperscript
RichTextMarked
RichTextCode
RichTextCustomEmoji
RichTextMathematicalExpression
RichTextUrl
RichTextEmailAddress
RichTextPhoneNumber
RichTextBankCardNumber
RichTextMention
RichTextHashtag
RichTextCashtag
RichTextBotCommand
RichTextAnchor
RichTextAnchorLink
RichTextReference
RichTextReferenceLink
RichTextBold
A bold text.

Field	Type	Description
type	String	Type of the rich text, always “bold”
text	RichText	The text
RichTextItalic
An italicized text.

Field	Type	Description
type	String	Type of the rich text, always “italic”
text	RichText	The text
RichTextUnderline
An underlined text.

Field	Type	Description
type	String	Type of the rich text, always “underline”
text	RichText	The text
RichTextStrikethrough
A strikethrough text.

Field	Type	Description
type	String	Type of the rich text, always “strikethrough”
text	RichText	The text
RichTextSpoiler
A text covered by a spoiler.

Field	Type	Description
type	String	Type of the rich text, always “spoiler”
text	RichText	The text
RichTextDateTime
Formatted date and time.

Field	Type	Description
type	String	Type of the rich text, always “date_time”
text	RichText	The text
unix_time	Integer	The Unix time associated with the entity
date_time_format	String	The string that defines the formatting of the date and time. See date-time entity formatting for more details.
RichTextTextMention
A mention of a Telegram user by their identifier.

Field	Type	Description
type	String	Type of the rich text, always “text_mention”
text	RichText	The text
user	User	The mentioned user
RichTextSubscript
A subscript text.

Field	Type	Description
type	String	Type of the rich text, always “subscript”
text	RichText	The text
RichTextSuperscript
A superscript text.

Field	Type	Description
type	String	Type of the rich text, always “superscript”
text	RichText	The text
RichTextMarked
A marked text.

Field	Type	Description
type	String	Type of the rich text, always “marked”
text	RichText	The text
RichTextCode
A monowidth text.

Field	Type	Description
type	String	Type of the rich text, always “code”
text	RichText	The text
RichTextCustomEmoji
A custom emoji.

Field	Type	Description
type	String	Type of the rich text, always “custom_emoji”
custom_emoji_id	String	Unique identifier of the custom emoji. Use getCustomEmojiStickers to get full information about the sticker.
alternative_text	String	Alternative emoji for the custom emoji
RichTextMathematicalExpression
A mathematical expression.

Field	Type	Description
type	String	Type of the rich text, always “mathematical_expression”
expression	String	The expression in LaTeX format
RichTextUrl
A text with a link.

Field	Type	Description
type	String	Type of the rich text, always “url”
text	RichText	The text
url	String	URL of the link
RichTextEmailAddress
A text with an email address.

Field	Type	Description
type	String	Type of the rich text, always “email_address”
text	RichText	The text
email_address	String	The email address
RichTextPhoneNumber
A text with a phone number.

Field	Type	Description
type	String	Type of the rich text, always “phone_number”
text	RichText	The text
phone_number	String	The phone number
RichTextBankCardNumber
A text with a bank card number.

Field	Type	Description
type	String	Type of the rich text, always “bank_card_number”
text	RichText	The text
bank_card_number	String	The bank card number
RichTextMention
A mention by a username.

Field	Type	Description
type	String	Type of the rich text, always “mention”
text	RichText	The text
username	String	The username
RichTextHashtag
A hashtag.

Field	Type	Description
type	String	Type of the rich text, always “hashtag”
text	RichText	The text
hashtag	String	The hashtag
RichTextCashtag
A cashtag.

Field	Type	Description
type	String	Type of the rich text, always “cashtag”
text	RichText	The text
cashtag	String	The cashtag
RichTextBotCommand
A bot command.

Field	Type	Description
type	String	Type of the rich text, always “bot_command”
text	RichText	The text
bot_command	String	The bot command
RichTextAnchor
An anchor.

Field	Type	Description
type	String	Type of the rich text, always “anchor”
name	String	The name of the anchor
RichTextAnchorLink
A link to an anchor.

Field	Type	Description
type	String	Type of the rich text, always “anchor_link”
text	RichText	The link text
anchor_name	String	The name of the anchor. If the name is empty, then the link brings back to the top of the message.
RichTextReference
A reference.

Field	Type	Description
type	String	Type of the rich text, always “reference”
text	RichText	Text of the reference
name	String	The name of the reference
RichTextReferenceLink
A link to a reference.

Field	Type	Description
type	String	Type of the rich text, always “reference_link”
text	RichText	The link text
reference_name	String	The name of the reference
RichBlockCaption
Caption of a rich formatted block.

Field	Type	Description
text	RichText	Block caption
credit	RichText	Optional. Block credit which corresponds to the HTML tag <cite>
RichBlockTableCell
Cell in a table.

Field	Type	Description
text	RichText	Optional. Text in the cell. If omitted, then the cell is invisible.
is_header	True	Optional. True, if the cell is a header cell
colspan	Integer	Optional. The number of columns the cell spans if it is bigger than 1
rowspan	Integer	Optional. The number of rows the cell spans if it is bigger than 1
align	String	Horizontal cell content alignment. Currently, must be one of “left”, “center”, or “right”.
valign	String	Vertical cell content alignment. Currently, must be one of “top”, “middle”, or “bottom”.
RichBlockListItem
An item of a list.

Field	Type	Description
label	String	Label of the item
blocks	Array of RichBlock	The content of the item
has_checkbox	True	Optional. True, if the item has a checkbox
is_checked	True	Optional. True, if the item has a checked checkbox
value	Integer	Optional. For ordered lists, the numeric value of the item label
type	String	Optional. For ordered lists, the type of the item label; must be one of “a” for lowercase letters, “A” for uppercase letters, “i” for lowercase Roman numerals, “I” for uppercase Roman numerals, or “1” for decimal numbers
RichBlock
This object represents a block in a rich formatted message. Currently, it can be any of the following types:

RichBlockParagraph
RichBlockSectionHeading
RichBlockPreformatted
RichBlockFooter
RichBlockDivider
RichBlockMathematicalExpression
RichBlockAnchor
RichBlockList
RichBlockBlockQuotation
RichBlockPullQuotation
RichBlockCollage
RichBlockSlideshow
RichBlockTable
RichBlockDetails
RichBlockMap
RichBlockAnimation
RichBlockAudio
RichBlockPhoto
RichBlockVideo
RichBlockVoiceNote
RichBlockThinking
RichBlockParagraph
A text paragraph, corresponding to the HTML tag <p>.

Field	Type	Description
type	String	Type of the block, always “paragraph”
text	RichText	Text of the block
RichBlockSectionHeading
A section heading, corresponding to the HTML tags <h1>, <h2>, <h3>, <h4>, <h5>, or <h6>.

Field	Type	Description
type	String	Type of the block, always “heading”
text	RichText	Text of the block
size	Integer	Relative size of the text font; 1-6, 1 is the largest, 6 is the smallest
RichBlockPreformatted
A preformatted text block, corresponding to the nested HTML tags <pre> and <code>.

Field	Type	Description
type	String	Type of the block, always “pre”
text	RichText	Text of the block
language	String	Optional. The programming language of the text
RichBlockFooter
A footer, corresponding to the HTML tag <footer>.

Field	Type	Description
type	String	Type of the block, always “footer”
text	RichText	Text of the block
RichBlockDivider
A divider, corresponding to the HTML tag <hr/>.

Field	Type	Description
type	String	Type of the block, always “divider”
RichBlockMathematicalExpression
A block with a mathematical expression in LaTeX format, corresponding to the custom HTML tag <tg-math-block>.

Field	Type	Description
type	String	Type of the block, always “mathematical_expression”
expression	String	The mathematical expression in LaTeX format
RichBlockAnchor
A block with an anchor, corresponding to the HTML tag <a> with the attribute name.

Field	Type	Description
type	String	Type of the block, always “anchor”
name	String	The name of the anchor
RichBlockList
A list of blocks, corresponding to the HTML tag <ul> or <ol> with multiple nested tags <li>.

Field	Type	Description
type	String	Type of the block, always “list”
items	Array of RichBlockListItem	Items of the list
RichBlockBlockQuotation
A block quotation, corresponding to the HTML tag <blockquote>.

Field	Type	Description
type	String	Type of the block, always “blockquote”
blocks	Array of RichBlock	Content of the block
credit	RichText	Optional. Credit of the block
RichBlockPullQuotation
A quotation with centered text, loosely corresponding to the HTML tag <aside>.

Field	Type	Description
type	String	Type of the block, always “pullquote”
text	RichText	Text of the block
credit	RichText	Optional. Credit of the block
RichBlockCollage
A collage, corresponding to the custom HTML tag <tg-collage>.

Field	Type	Description
type	String	Type of the block, always “collage”
blocks	Array of RichBlock	Elements of the collage
caption	RichBlockCaption	Optional. Caption of the block
RichBlockSlideshow
A slideshow, corresponding to the custom HTML tag <tg-slideshow>.

Field	Type	Description
type	String	Type of the block, always “slideshow”
blocks	Array of RichBlock	Elements of the slideshow
caption	RichBlockCaption	Optional. Caption of the block
RichBlockTable
A table, corresponding to the HTML tag <table>.

Field	Type	Description
type	String	Type of the block, always “table”
cells	Array of Array of RichBlockTableCell	Cells of the table
is_bordered	True	Optional. True, if the table has borders
is_striped	True	Optional. True, if the table is striped
caption	RichText	Optional. Caption of the table
RichBlockDetails
An expandable block for details disclosure, corresponding to the HTML tag <details>.

Field	Type	Description
type	String	Type of the block, always “details”
summary	RichText	Always shown summary of the block
blocks	Array of RichBlock	Content of the block
is_open	True	Optional. True, if the content of the block is visible by default
RichBlockMap
A block with a map, corresponding to the custom HTML tag <tg-map>.

Field	Type	Description
type	String	Type of the block, always “map”
location	Location	Location of the center of the map
zoom	Integer	Map zoom level; 13-20
width	Integer	Expected width of the map
height	Integer	Expected height of the map
caption	RichBlockCaption	Optional. Caption of the block
RichBlockAnimation
A block with an animation, corresponding to the HTML tag <video>.

Field	Type	Description
type	String	Type of the block, always “animation”
animation	Animation	The animation
has_spoiler	True	Optional. True, if the media preview is covered by a spoiler animation
caption	RichBlockCaption	Optional. Caption of the block
RichBlockAudio
A block with a music file, corresponding to the HTML tag <audio>.

Field	Type	Description
type	String	Type of the block, always “audio”
audio	Audio	The audio
caption	RichBlockCaption	Optional. Caption of the block
RichBlockPhoto
A block with a photo, corresponding to the HTML tag <photo>.

Field	Type	Description
type	String	Type of the block, always “photo”
photo	Array of PhotoSize	Available sizes of the photo
has_spoiler	True	Optional. True, if the media preview is covered by a spoiler animation
caption	RichBlockCaption	Optional. Caption of the block
RichBlockVideo
A block with a video, corresponding to the HTML tag <video>.

Field	Type	Description
type	String	Type of the block, always “video”
video	Video	The video
has_spoiler	True	Optional. True, if the media preview is covered by a spoiler animation
caption	RichBlockCaption	Optional. Caption of the block
RichBlockVoiceNote
A block with a voice note, corresponding to the HTML tag <audio>.

Field	Type	Description
type	String	Type of the block, always “voice_note”
voice_note	Voice	The voice note
caption	RichBlockCaption	Optional. Caption of the block
RichBlockThinking
A block with a “Thinking…” placeholder, corresponding to the custom HTML tag <tg-thinking>. The block may be used only in sendRichMessageDraft, therefore it can't be received in messages. See https://t.me/addemoji/AIActions for examples of custom emoji, which are recommended for usage in the block.

Field	Type	Description
type	String	Type of the block, always “thinking”
text	RichText	Text of the block. See https://t.me/addemoji/AIActions for examples of custom emoji, which are recommended for usage in the block.
Inline mode
The following methods and objects allow your bot to work in inline mode.
Please see our Introduction to Inline bots for more details.

To enable this option, send the /setinline command to @BotFather and provide the placeholder text that the user will see in the input field after typing your bot's name.

InlineQuery
This object represents an incoming inline query. When the user sends an empty query, your bot could return some default or trending results.

Field	Type	Description
id	String	Unique identifier for this query
from	User	Sender
query	String	Text of the query (up to 256 characters)
offset	String	Offset of the results to be returned, can be controlled by the bot
chat_type	String	Optional. Type of the chat from which the inline query was sent. Can be either “sender” for a private chat with the inline query sender, “private”, “group”, “supergroup”, or “channel”. The chat type should be always known for requests sent from official clients and most third-party clients, unless the request was sent from a secret chat.
location	Location	Optional. Sender location, only for bots that request user location
answerInlineQuery
Use this method to send answers to an inline query. On success, True is returned.
No more than 50 results per query are allowed.

Parameter	Type	Required	Description
inline_query_id	String	Yes	Unique identifier for the answered query
results	Array of InlineQueryResult	Yes	A JSON-serialized array of results for the inline query
cache_time	Integer	Optional	The maximum amount of time in seconds that the result of the inline query may be cached on the server. Defaults to 300.
is_personal	Boolean	Optional	Pass True if results may be cached on the server side only for the user that sent the query. By default, results may be returned to any user who sends the same query.
next_offset	String	Optional	Pass the offset that a client should send in the next query with the same text to receive more results. Pass an empty string if there are no more results or if you don't support pagination. Offset length can't exceed 64 bytes.
button	InlineQueryResultsButton	Optional	A JSON-serialized object describing a button to be shown above inline query results
InlineQueryResultsButton
This object represents a button to be shown above inline query results. You must use exactly one of the optional fields.

Field	Type	Description
text	String	Label text on the button
web_app	WebAppInfo	Optional. Description of the Web App that will be launched when the user presses the button. The Web App will be able to switch back to the inline mode using the method switchInlineQuery inside the Web App.
start_parameter	String	Optional. Deep-linking parameter for the /start message sent to the bot when a user presses the button. 1-64 characters, only A-Z, a-z, 0-9, _ and - are allowed.

Example: An inline bot that sends YouTube videos can ask the user to connect the bot to their YouTube account to adapt search results accordingly. To do this, it displays a 'Connect your YouTube account' button above the results, or even before showing any. The user presses the button, switches to a private chat with the bot and, in doing so, passes a start parameter that instructs the bot to return an OAuth link. Once done, the bot can offer a switch_inline button so that the user can easily return to the chat where they wanted to use the bot's inline capabilities.
InlineQueryResult
This object represents one result of an inline query. Telegram clients currently support results of the following 20 types:

InlineQueryResultCachedAudio
InlineQueryResultCachedDocument
InlineQueryResultCachedGif
InlineQueryResultCachedMpeg4Gif
InlineQueryResultCachedPhoto
InlineQueryResultCachedSticker
InlineQueryResultCachedVideo
InlineQueryResultCachedVoice
InlineQueryResultArticle
InlineQueryResultAudio
InlineQueryResultContact
InlineQueryResultGame
InlineQueryResultDocument
InlineQueryResultGif
InlineQueryResultLocation
InlineQueryResultMpeg4Gif
InlineQueryResultPhoto
InlineQueryResultVenue
InlineQueryResultVideo
InlineQueryResultVoice
Note: All URLs passed in inline query results will be available to end users and therefore must be assumed to be public.

InlineQueryResultArticle
Represents a link to an article or web page.

Field	Type	Description
type	String	Type of the result, must be article
id	String	Unique identifier for this result, 1-64 Bytes
title	String	Title of the result
input_message_content	InputMessageContent	Content of the message to be sent
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
url	String	Optional. URL of the result
description	String	Optional. Short description of the result
thumbnail_url	String	Optional. Url of the thumbnail for the result
thumbnail_width	Integer	Optional. Thumbnail width
thumbnail_height	Integer	Optional. Thumbnail height
InlineQueryResultPhoto
Represents a link to a photo. By default, this photo will be sent by the user with optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the photo.

Field	Type	Description
type	String	Type of the result, must be photo
id	String	Unique identifier for this result, 1-64 bytes
photo_url	String	A valid URL of the photo. Photo must be in JPEG format. Photo size must not exceed 5MB.
thumbnail_url	String	URL of the thumbnail for the photo
photo_width	Integer	Optional. Width of the photo
photo_height	Integer	Optional. Height of the photo
title	String	Optional. Title for the result
description	String	Optional. Short description of the result
caption	String	Optional. Caption of the photo to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the photo caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the photo
InlineQueryResultGif
Represents a link to an animated GIF file. By default, this animated GIF file will be sent by the user with optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the animation.

Field	Type	Description
type	String	Type of the result, must be gif
id	String	Unique identifier for this result, 1-64 bytes
gif_url	String	A valid URL for the GIF file
gif_width	Integer	Optional. Width of the GIF
gif_height	Integer	Optional. Height of the GIF
gif_duration	Integer	Optional. Duration of the GIF in seconds
thumbnail_url	String	URL of the static (JPEG or GIF) or animated (MPEG4) thumbnail for the result
thumbnail_mime_type	String	Optional. MIME type of the thumbnail, must be one of “image/jpeg”, “image/gif”, or “video/mp4”. Defaults to “image/jpeg”.
title	String	Optional. Title for the result
caption	String	Optional. Caption of the GIF file to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the GIF animation
InlineQueryResultMpeg4Gif
Represents a link to a video animation (H.264/MPEG-4 AVC video without sound). By default, this animated MPEG-4 file will be sent by the user with optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the animation.

Field	Type	Description
type	String	Type of the result, must be mpeg4_gif
id	String	Unique identifier for this result, 1-64 bytes
mpeg4_url	String	A valid URL for the MPEG4 file
mpeg4_width	Integer	Optional. Video width
mpeg4_height	Integer	Optional. Video height
mpeg4_duration	Integer	Optional. Video duration in seconds
thumbnail_url	String	URL of the static (JPEG or GIF) or animated (MPEG4) thumbnail for the result
thumbnail_mime_type	String	Optional. MIME type of the thumbnail, must be one of “image/jpeg”, “image/gif”, or “video/mp4”. Defaults to “image/jpeg”.
title	String	Optional. Title for the result
caption	String	Optional. Caption of the MPEG-4 file to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the video animation
InlineQueryResultVideo
Represents a link to a page containing an embedded video player or a video file. By default, this video file will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the video.

If an InlineQueryResultVideo message contains an embedded video (e.g., YouTube), you must replace its content using input_message_content.

Field	Type	Description
type	String	Type of the result, must be video
id	String	Unique identifier for this result, 1-64 bytes
video_url	String	A valid URL for the embedded video player or video file
mime_type	String	MIME type of the content of the video URL, “text/html” or “video/mp4”
thumbnail_url	String	URL of the thumbnail (JPEG only) for the video
title	String	Title for the result
caption	String	Optional. Caption of the video to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the video caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
video_width	Integer	Optional. Video width
video_height	Integer	Optional. Video height
video_duration	Integer	Optional. Video duration in seconds
description	String	Optional. Short description of the result
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the video. This field is required if InlineQueryResultVideo is used to send an HTML-page as a result (e.g., a YouTube video).
InlineQueryResultAudio
Represents a link to an MP3 audio file. By default, this audio file will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the audio.

Field	Type	Description
type	String	Type of the result, must be audio
id	String	Unique identifier for this result, 1-64 bytes
audio_url	String	A valid URL for the audio file
title	String	Title
caption	String	Optional. Caption, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the audio caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
performer	String	Optional. Performer
audio_duration	Integer	Optional. Audio duration in seconds
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the audio
InlineQueryResultVoice
Represents a link to a voice recording in an .OGG container encoded with OPUS. By default, this voice recording will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the the voice message.

Field	Type	Description
type	String	Type of the result, must be voice
id	String	Unique identifier for this result, 1-64 bytes
voice_url	String	A valid URL for the voice recording
title	String	Recording title
caption	String	Optional. Caption, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the voice message caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
voice_duration	Integer	Optional. Recording duration in seconds
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the voice recording
InlineQueryResultDocument
Represents a link to a file. By default, this file will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the file. Currently, only .PDF and .ZIP files can be sent using this method.

Field	Type	Description
type	String	Type of the result, must be document
id	String	Unique identifier for this result, 1-64 bytes
title	String	Title for the result
caption	String	Optional. Caption of the document to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the document caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
document_url	String	A valid URL for the file
mime_type	String	MIME type of the content of the file, either “application/pdf” or “application/zip”
description	String	Optional. Short description of the result
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the file
thumbnail_url	String	Optional. URL of the thumbnail (JPEG only) for the file
thumbnail_width	Integer	Optional. Thumbnail width
thumbnail_height	Integer	Optional. Thumbnail height
InlineQueryResultLocation
Represents a location on a map. By default, the location will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the location.

Field	Type	Description
type	String	Type of the result, must be location
id	String	Unique identifier for this result, 1-64 Bytes
latitude	Float	Location latitude in degrees
longitude	Float	Location longitude in degrees
title	String	Location title
horizontal_accuracy	Float	Optional. The radius of uncertainty for the location, measured in meters; 0-1500
live_period	Integer	Optional. Period in seconds during which the location can be updated, must be between 60 and 86400, or 0x7FFFFFFF for live locations that can be edited indefinitely
heading	Integer	Optional. For live locations, a direction in which the user is moving, in degrees. Must be between 1 and 360 if specified.
proximity_alert_radius	Integer	Optional. For live locations, a maximum distance for proximity alerts about approaching another chat member, in meters. Must be between 1 and 100000 if specified.
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the location
thumbnail_url	String	Optional. Url of the thumbnail for the result
thumbnail_width	Integer	Optional. Thumbnail width
thumbnail_height	Integer	Optional. Thumbnail height
InlineQueryResultVenue
Represents a venue. By default, the venue will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the venue.

Field	Type	Description
type	String	Type of the result, must be venue
id	String	Unique identifier for this result, 1-64 Bytes
latitude	Float	Latitude of the venue location in degrees
longitude	Float	Longitude of the venue location in degrees
title	String	Title of the venue
address	String	Address of the venue
foursquare_id	String	Optional. Foursquare identifier of the venue if known
foursquare_type	String	Optional. Foursquare type of the venue, if known. (For example, “arts_entertainment/default”, “arts_entertainment/aquarium” or “food/icecream”.)
google_place_id	String	Optional. Google Places identifier of the venue
google_place_type	String	Optional. Google Places type of the venue. (See supported types.)
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the venue
thumbnail_url	String	Optional. Url of the thumbnail for the result
thumbnail_width	Integer	Optional. Thumbnail width
thumbnail_height	Integer	Optional. Thumbnail height
InlineQueryResultContact
Represents a contact with a phone number. By default, this contact will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the contact.

Field	Type	Description
type	String	Type of the result, must be contact
id	String	Unique identifier for this result, 1-64 Bytes
phone_number	String	Contact's phone number
first_name	String	Contact's first name
last_name	String	Optional. Contact's last name
vcard	String	Optional. Additional data about the contact in the form of a vCard, 0-2048 bytes
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the contact
thumbnail_url	String	Optional. Url of the thumbnail for the result
thumbnail_width	Integer	Optional. Thumbnail width
thumbnail_height	Integer	Optional. Thumbnail height
InlineQueryResultGame
Represents a Game.

Field	Type	Description
type	String	Type of the result, must be game
id	String	Unique identifier for this result, 1-64 bytes
game_short_name	String	Short name of the game
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
InlineQueryResultCachedPhoto
Represents a link to a photo stored on the Telegram servers. By default, this photo will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the photo.

Field	Type	Description
type	String	Type of the result, must be photo
id	String	Unique identifier for this result, 1-64 bytes
photo_file_id	String	A valid file identifier of the photo
title	String	Optional. Title for the result
description	String	Optional. Short description of the result
caption	String	Optional. Caption of the photo to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the photo caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the photo
InlineQueryResultCachedGif
Represents a link to an animated GIF file stored on the Telegram servers. By default, this animated GIF file will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with specified content instead of the animation.

Field	Type	Description
type	String	Type of the result, must be gif
id	String	Unique identifier for this result, 1-64 bytes
gif_file_id	String	A valid file identifier for the GIF file
title	String	Optional. Title for the result
caption	String	Optional. Caption of the GIF file to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the GIF animation
InlineQueryResultCachedMpeg4Gif
Represents a link to a video animation (H.264/MPEG-4 AVC video without sound) stored on the Telegram servers. By default, this animated MPEG-4 file will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the animation.

Field	Type	Description
type	String	Type of the result, must be mpeg4_gif
id	String	Unique identifier for this result, 1-64 bytes
mpeg4_file_id	String	A valid file identifier for the MPEG4 file
title	String	Optional. Title for the result
caption	String	Optional. Caption of the MPEG-4 file to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the video animation
InlineQueryResultCachedSticker
Represents a link to a sticker stored on the Telegram servers. By default, this sticker will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the sticker.

Field	Type	Description
type	String	Type of the result, must be sticker
id	String	Unique identifier for this result, 1-64 bytes
sticker_file_id	String	A valid file identifier of the sticker
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the sticker
InlineQueryResultCachedDocument
Represents a link to a file stored on the Telegram servers. By default, this file will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the file.

Field	Type	Description
type	String	Type of the result, must be document
id	String	Unique identifier for this result, 1-64 bytes
title	String	Title for the result
document_file_id	String	A valid file identifier for the file
description	String	Optional. Short description of the result
caption	String	Optional. Caption of the document to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the document caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the file
InlineQueryResultCachedVideo
Represents a link to a video file stored on the Telegram servers. By default, this video file will be sent by the user with an optional caption. Alternatively, you can use input_message_content to send a message with the specified content instead of the video.

Field	Type	Description
type	String	Type of the result, must be video
id	String	Unique identifier for this result, 1-64 bytes
video_file_id	String	A valid file identifier for the video file
title	String	Title for the result
description	String	Optional. Short description of the result
caption	String	Optional. Caption of the video to be sent, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the video caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
show_caption_above_media	Boolean	Optional. Pass True, if the caption must be shown above the message media
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the video
InlineQueryResultCachedVoice
Represents a link to a voice message stored on the Telegram servers. By default, this voice message will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the voice message.

Field	Type	Description
type	String	Type of the result, must be voice
id	String	Unique identifier for this result, 1-64 bytes
voice_file_id	String	A valid file identifier for the voice message
title	String	Voice message title
caption	String	Optional. Caption, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the voice message caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the voice message
InlineQueryResultCachedAudio
Represents a link to an MP3 audio file stored on the Telegram servers. By default, this audio file will be sent by the user. Alternatively, you can use input_message_content to send a message with the specified content instead of the audio.

Field	Type	Description
type	String	Type of the result, must be audio
id	String	Unique identifier for this result, 1-64 bytes
audio_file_id	String	A valid file identifier for the audio file
caption	String	Optional. Caption, 0-1024 characters after entities parsing
parse_mode	String	Optional. Mode for parsing entities in the audio caption. See formatting options for more details.
caption_entities	Array of MessageEntity	Optional. List of special entities that appear in the caption, which can be specified instead of parse_mode
reply_markup	InlineKeyboardMarkup	Optional. Inline keyboard attached to the message
input_message_content	InputMessageContent	Optional. Content of the message to be sent instead of the audio
InputMessageContent
This object represents the content of a message to be sent as a result of an inline query. Telegram clients currently support the following types:

InputTextMessageContent
InputRichMessageContent
InputLocationMessageContent
InputVenueMessageContent
InputContactMessageContent
InputInvoiceMessageContent
InputTextMessageContent
Represents the content of a text message to be sent as the result of an inline query.

Field	Type	Description
message_text	String	Text of the message to be sent, 1-4096 characters
parse_mode	String	Optional. Mode for parsing entities in the message text. See formatting options for more details.
entities	Array of MessageEntity	Optional. List of special entities that appear in message text, which can be specified instead of parse_mode
link_preview_options	LinkPreviewOptions	Optional. Link preview generation options for the message
InputRichMessageContent
Represents the content of a rich message to be sent as the result of an inline query.

Field	Type	Description
rich_message	InputRichMessage	The message to be sent
InputLocationMessageContent
Represents the content of a location message to be sent as the result of an inline query.

Field	Type	Description
latitude	Float	Latitude of the location in degrees
longitude	Float	Longitude of the location in degrees
horizontal_accuracy	Float	Optional. The radius of uncertainty for the location, measured in meters; 0-1500
live_period	Integer	Optional. Period in seconds during which the location can be updated, must be between 60 and 86400, or 0x7FFFFFFF for live locations that can be edited indefinitely
heading	Integer	Optional. For live locations, a direction in which the user is moving, in degrees. Must be between 1 and 360 if specified.
proximity_alert_radius	Integer	Optional. For live locations, a maximum distance for proximity alerts about approaching another chat member, in meters. Must be between 1 and 100000 if specified.
InputVenueMessageContent
Represents the content of a venue message to be sent as the result of an inline query.

Field	Type	Description
latitude	Float	Latitude of the venue in degrees
longitude	Float	Longitude of the venue in degrees
title	String	Name of the venue
address	String	Address of the venue
foursquare_id	String	Optional. Foursquare identifier of the venue, if known
foursquare_type	String	Optional. Foursquare type of the venue, if known. (For example, “arts_entertainment/default”, “arts_entertainment/aquarium” or “food/icecream”.)
google_place_id	String	Optional. Google Places identifier of the venue
google_place_type	String	Optional. Google Places type of the venue. (See supported types.)
InputContactMessageContent
Represents the content of a contact message to be sent as the result of an inline query.

Field	Type	Description
phone_number	String	Contact's phone number
first_name	String	Contact's first name
last_name	String	Optional. Contact's last name
vcard	String	Optional. Additional data about the contact in the form of a vCard, 0-2048 bytes
InputInvoiceMessageContent
Represents the content of an invoice message to be sent as the result of an inline query.

Field	Type	Description
title	String	Product name, 1-32 characters
description	String	Product description, 1-255 characters
payload	String	Bot-defined invoice payload, 1-128 bytes. This will not be displayed to the user, use it for your internal processes.
provider_token	String	Optional. Payment provider token, obtained via @BotFather. Pass an empty string for payments in Telegram Stars.
currency	String	Three-letter ISO 4217 currency code, see more on currencies. Pass “XTR” for payments in Telegram Stars.
prices	Array of LabeledPrice	Price breakdown, a JSON-serialized list of components (e.g. product price, tax, discount, delivery cost, delivery tax, bonus, etc.). Must contain exactly one item for payments in Telegram Stars.
max_tip_amount	Integer	Optional. The maximum accepted amount for tips in the smallest units of the currency (integer, not float/double). For example, for a maximum tip of US$ 1.45 pass max_tip_amount = 145. See the exp parameter in currencies.json, it shows the number of digits past the decimal point for each currency (2 for the majority of currencies). Defaults to 0. Not supported for payments in Telegram Stars.
suggested_tip_amounts	Array of Integer	Optional. A JSON-serialized array of suggested amounts of tip in the smallest units of the currency (integer, not float/double). At most 4 suggested tip amounts can be specified. The suggested tip amounts must be positive, passed in a strictly increased order and must not exceed max_tip_amount.
provider_data	String	Optional. A JSON-serialized object for data about the invoice, which will be shared with the payment provider. A detailed description of the required fields should be provided by the payment provider.
photo_url	String	Optional. URL of the product photo for the invoice. Can be a photo of the goods or a marketing image for a service.
photo_size	Integer	Optional. Photo size in bytes
photo_width	Integer	Optional. Photo width
photo_height	Integer	Optional. Photo height
need_name	Boolean	Optional. Pass True if you require the user's full name to complete the order. Ignored for payments in Telegram Stars.
need_phone_number	Boolean	Optional. Pass True if you require the user's phone number to complete the order. Ignored for payments in Telegram Stars.
need_email	Boolean	Optional. Pass True if you require the user's email address to complete the order. Ignored for payments in Telegram Stars.
need_shipping_address	Boolean	Optional. Pass True if you require the user's shipping address to complete the order. Ignored for payments in Telegram Stars.
send_phone_number_to_provider	Boolean	Optional. Pass True if the user's phone number should be sent to the provider. Ignored for payments in Telegram Stars.
send_email_to_provider	Boolean	Optional. Pass True if the user's email address should be sent to the provider. Ignored for payments in Telegram Stars.
is_flexible	Boolean	Optional. Pass True if the final price depends on the shipping method. Ignored for payments in Telegram Stars.
ChosenInlineResult
Represents a result of an inline query that was chosen by the user and sent to their chat partner.

Field	Type	Description
result_id	String	The unique identifier for the result that was chosen
from	User	The user that chose the result
location	Location	Optional. Sender location, only for bots that require user location
inline_message_id	String	Optional. Identifier of the sent inline message. Available only if there is an inline keyboard attached to the message. Will be also received in callback queries and can be used to edit the message.
query	String	The query that was used to obtain the result
Note: It is necessary to enable inline feedback via @BotFather in order to receive these objects in updates.