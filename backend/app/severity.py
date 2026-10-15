"""对账判词的三档严重度：只有一个出处。

以前 load / pending / memory / storage 四个对账服务各抄一份同样的四行，第五个（登录安全）
另立了最轻那一档的名字（"note"）。抄的时候没人核对，改一处措辞就得改四处；更要紧的是
前端按档分组是把值当条件用的（存储页 `severity !== 'info'`、登录页 counts 里的 note），
新页签照抄错一个词，判词就会静默落错分组，而两页各自看着都"正常"。

`info` 与 `note` 都保留：它们已经是线上 JSON 里的值，改名等于同时改接口。这里收的是
**名字与说法**——写判词用常量，不再拼字符串；要显示用 SEV_LABEL，一处措辞。
新页签请沿用 info。
"""

SEV_RISK = "risk"
SEV_WARN = "warn"
SEV_INFO = "info"
SEV_NOTE = "note"

SEV_LABEL = {
    SEV_RISK: "要紧",
    SEV_WARN: "值得处理",
    SEV_INFO: "知道就好",
    SEV_NOTE: "知道就好",
}

# 排序用：越靠前越要紧。两档"知道就好"排在一起，避免两个页签的同级判词差一位。
SEV_ORDER = {SEV_RISK: 0, SEV_WARN: 1, SEV_INFO: 2, SEV_NOTE: 2}
