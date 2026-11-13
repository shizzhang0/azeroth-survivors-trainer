-- AZS Remote: in-game panel for the azeroth-survivors-trainer agent.
--
-- The Lua sandbox cannot reach any outside program (io/os/require are nil and
-- LaunchURL is gone inside a run), so the agent (remote.bat -> src/agent.py)
-- comes to us: it finds AZSRemote_MB in memory by its magic numbers and reads
-- and writes its slots. This file only fills in requests and shows replies.
--
-- Slot numbers and the field list must match src/addon_mailbox.py and
-- src/session.py FIELDS — tests/test_offline.py checks both.

local PROTOCOL = 7;      -- bump with any slot or field-order change, as in src/addon_mailbox.py
local SLOT = { REQ = 4, CMD = 5, ARG1 = 6, ARG2 = 7, ACK = 8, RESULT = 9,
	HEARTBEAT = 10, STATE = 11, LUA_BEAT = 12, VALUES = 13, LOCKED = 42,
	TURBO_SPEED = 71, TURBO_STATE = 72, TURBO_TIME = 73, FULL_LOCKED = 74, SIZE = 74 };
local CMD = { WRITE = 1, LOCK = 2, UNLOCK = 3, HEAL = 4, UNLOCK_ALL = 5, RELOCATE = 6, TURBO = 7,
	KEEP_FULL = 8 };

-- kind, name, label, format ("i" whole number / "f" decimal), lockable, unit, group
-- Labels, order and groups follow the game's character pane; unit says how the
-- pane writes the value (see src/session.py), and the panel shows and takes
-- input the same way.
local FIELDS = {
	{ "stat", "hp", "生命值", "f", true, "num", "角色" },
	{ "stat", "dmg", "伤害", "f", true, "+%1", "角色" },
	{ "stat", "cd", "急速", "f", true, "haste", "角色" },
	{ "stat", "crit", "暴击", "f", true, "%.1", "角色" },
	{ "stat", "cm", "暴击伤害", "f", true, "%", "角色" },
	{ "stat", "arm", "护甲", "f", true, "%", "角色" },
	{ "stat", "area", "范围", "f", true, "+%1", "角色" },
	{ "stat", "targets", "目标数", "i", true, "+int", "角色" },
	{ "stat", "echo", "回响", "f", true, "%", "角色" },
	{ "stat", "spd", "速度", "f", true, "+%1", "角色" },
	{ "stat", "reg", "回复", "f", true, "num", "角色" },
	{ "stat", "pick", "拾取（码）", "f", true, "num", "角色" },
	{ "effect", "lifesteal", "生命偷取（不含基础）", "f", true, "%.1", "冒险属性" },
	{ "stat", "heal", "击杀治疗", "f", true, "num", "冒险属性" },
	{ "effect", "thorns", "荆棘", "f", true, "%", "冒险属性" },
	{ "stat", "lives", "生命数", "i", true, "int", "冒险属性" },
	{ "stat", "xp", "经验值", "f", true, "+%1", "冒险属性" },
	{ "stat", "gold", "金币", "f", true, "+%1", "冒险属性" },
	{ "stat", "luck", "幸运", "f", true, "+%", "冒险属性" },
	{ "stat", "bp", "增益强度", "f", true, "+%", "冒险属性" },
	{ "stat", "dots", "持续伤害层数", "i", true, "dots", "冒险属性" },
	{ "stat", "doth", "持续伤害急速", "f", true, "+%", "冒险属性" },
	{ "player", "gold", "金币数量", "i", false, "int", "状态" },
	{ "player", "level", "等级", "i", false, "int", "状态" },
	{ "player", "xp", "当前经验", "i", false, "int", "状态" },
	{ "player", "kills", "击杀", "i", false, "int", "状态" },
	{ "player", "rerolls", "重抽次数", "i", false, "int", "状态" },
	{ "player", "banishes", "放逐次数", "i", false, "int", "状态" },
	{ "player", "chest", "宝箱价格", "i", true, "int", "状态" },
};

-- The mailbox. Magic numbers come from tonumber() at runtime, so they exist
-- only here and not as constants of this chunk.
AZSRemote_MB = {};
for i = 1, SLOT.SIZE do
	AZSRemote_MB[i] = 0;
end
AZSRemote_MB[1] = tonumber("918273.645");
AZSRemote_MB[2] = tonumber("-556677.889");
AZSRemote_MB[3] = tonumber("1234000") + 1000 * PROTOCOL + table.getn(FIELDS);   -- layout tag
local MB = AZSRemote_MB;

local queue = {};
local lastBeat, lastBeatAt = 0, 0;
local waitingFor = nil;          -- request number we are waiting a reply for
local rows = {};

local function say(msg)
	if ( DEFAULT_CHAT_FRAME ) then
		DEFAULT_CHAT_FRAME:AddMessage("|cffffd100[AZS]|r " .. msg, 1, 1, 1);
	end
end

local function send(cmd, field, value)
	table.insert(queue, { cmd, field or 0, value or 0 });
end

-- --- game units <-> raw values (mirrors to_game/from_game/show/parse in session.py)
local DOTS_MAX_EXTRA = 3;   -- the game caps extra DoT stacks at 3 (4 shown)

local function toGame(unit, v)
	if ( unit == "dots" ) then
		return math.min(math.max(v, 0), DOTS_MAX_EXTRA) + 1;
	end
	if ( unit == "%" or unit == "%.1" or unit == "+%" ) then
		return v * 100;
	elseif ( unit == "+%1" ) then
		return (v - 1) * 100;
	elseif ( unit == "haste" ) then
		return (1 - v) * 100;
	end
	return v;
end

local function fromGame(unit, x)
	if ( unit == "dots" ) then
		return math.min(math.max(x - 1, 0), DOTS_MAX_EXTRA);
	end
	if ( unit == "%" or unit == "%.1" or unit == "+%" ) then
		return x / 100;
	elseif ( unit == "+%1" ) then
		return 1 + x / 100;
	elseif ( unit == "haste" ) then
		return 1 - x / 100;
	end
	return x;
end

local function round(x)
	return math.floor(x + 0.5);
end

-- raw value -> text like the game pane's: "+73%", "26.4%", "+5", "447"
function AZSRemote_Show(unit, v)
	local x = toGame(unit, v);
	local s;
	if ( unit == "%.1" ) then                -- the pane gives crit one decimal: 26.4%
		s = string.format("%.1f", x);
	elseif ( unit == "num" and math.abs(x - round(x)) >= 0.05 ) then
		s = string.format("%.1f", x);        -- 回复 3.3
	else
		s = string.format("%d", round(x));   -- every other percentage is whole
	end
	if ( (unit == "+%" or unit == "+%1" or unit == "haste" or unit == "+int")
			and x >= 0 and string.sub(s, 1, 1) ~= "-" ) then
		s = "+" .. s;
	end
	if ( string.find(unit, "%", 1, true) or unit == "haste" ) then
		s = s .. "%";
	end
	return s;
end

-- what you typed, the way the pane writes it ("+50%", "50", "-10") -> raw value
function AZSRemote_Parse(unit, text)
	local t = string.gsub(text or "", "[%%%+%s]", "");
	local x = tonumber(t);
	if ( not x ) then
		return nil;
	end
	return fromGame(unit, x);
end

-- --- panel -----------------------------------------------------------------
-- Laid out like the game's character pane: 角色 | 冒险属性 side by side, 状态 below
-- in two columns of four, then 极速模式 and the buttons on one line. Calls that only
-- decorate go through try(): if this client lacks one, the panel still works.
local function try(f, a, b, c, d, e, g)
	if ( f ) then
		pcall(f, a, b, c, d, e, g);
	end
end

local PAD, COL_W, GAP, ROW_H, TOP = 24, 380, 24, 21, -58;
local panel = CreateFrame("Frame", "AZSRemoteFrame", UIParent);
panel:SetWidth(PAD * 2 + COL_W * 2 + GAP);
panel:SetHeight(520);
panel:SetPoint("CENTER", UIParent, "CENTER", 0, 0);
panel:SetFrameStrata("DIALOG");
panel:SetBackdrop({
	bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
	edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
	tile = true, tileSize = 32, edgeSize = 32,
	insets = { left = 11, right = 12, top = 12, bottom = 11 } });
panel:EnableMouse(true);
panel:SetMovable(true);
panel:RegisterForDrag("LeftButton");
panel:SetScript("OnDragStart", function() this:StartMoving(); end);
panel:SetScript("OnDragStop", function() this:StopMovingOrSizing(); end);
panel:Hide();
if ( UISpecialFrames ) then                 -- Esc closes it, like the game's own panels
	try(table.insert, UISpecialFrames, "AZSRemoteFrame");
end

local title = panel:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge");
title:SetPoint("TOPLEFT", panel, "TOPLEFT", PAD, -22);
title:SetText("AZS 控制器");

local status = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
status:SetPoint("TOPLEFT", panel, "TOPLEFT", PAD + 110, -26);
status:SetText("");

local close = CreateFrame("Button", "AZSRemoteClose", panel, "UIPanelCloseButton");
close:SetPoint("TOPRIGHT", panel, "TOPRIGHT", -6, -6);

-- a gold heading with a faint rule under it, w wide
local function heading(text, x, y, w)
	local h = panel:CreateFontString(nil, "OVERLAY", "GameFontNormal");
	h:SetPoint("TOPLEFT", panel, "TOPLEFT", x, y);
	h:SetText(text);
	local ok, line = pcall(panel.CreateTexture, panel, nil, "ARTWORK");
	if ( ok and line ) then
		try(line.SetTexture, line, 1, 0.82, 0, 0.3);
		try(line.SetHeight, line, 1);
		try(line.SetWidth, line, w);
		try(line.SetPoint, line, "TOPLEFT", panel, "TOPLEFT", x, y - 17);
	end
	return h;
end

local function paintValue(row, locked)
	if ( row.green ~= locked ) then          -- locked values in green, the rest white
		if ( locked ) then
			try(row.cur.SetTextColor, row.cur, 0.4, 1, 0.4);
		else
			try(row.cur.SetTextColor, row.cur, 1, 1, 1);
		end
		row.green = locked;
	end
end

-- one row at column x: label · current value · input · [写] · [锁]
local function addRow(i, x, y)
	local kind, label, lockable, unit = FIELDS[i][1], FIELDS[i][3], FIELDS[i][5], FIELDS[i][6];
	local row = {};
	local l = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
	l:SetPoint("TOPLEFT", panel, "TOPLEFT", x, y);
	l:SetText(label);
	try(l.SetTextColor, l, 0.82, 0.82, 0.82);
	row.cur = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
	row.cur:SetPoint("TOPRIGHT", panel, "TOPLEFT", x + 196, y);
	row.cur:SetJustifyH("RIGHT");
	row.cur:SetText("-");

	local eb = CreateFrame("EditBox", "AZSRemoteEdit" .. i, panel, "InputBoxTemplate");
	eb:SetWidth(62);
	eb:SetHeight(18);
	eb:SetPoint("TOPLEFT", panel, "TOPLEFT", x + 210, y + 3);
	eb:SetAutoFocus(false);
	eb:SetScript("OnEscapePressed", function() this:ClearFocus(); end);
	row.edit = eb;

	-- typed in the game's units; sent raw. Empty or junk: keep the current value.
	local function value()
		local v = AZSRemote_Parse(unit, eb:GetText());
		if ( not v ) then
			v = MB[SLOT.VALUES + i - 1];
			eb:SetText(AZSRemote_Show(unit, v));
		end
		return v;
	end

	local lx = x + 280;
	if ( kind == "player" ) then
		local b = CreateFrame("Button", "AZSRemoteWrite" .. i, panel, "UIPanelButtonTemplate");
		b:SetWidth(36);
		b:SetHeight(18);
		b:SetPoint("TOPLEFT", panel, "TOPLEFT", x + 280, y + 3);
		b:SetText("写");
		b:SetScript("OnClick", function() send(CMD.WRITE, i, value()); end);
		lx = x + 320;
	end
	if ( lockable ) then
		local c = CreateFrame("CheckButton", "AZSRemoteLock" .. i, panel, "UICheckButtonTemplate");
		c:SetWidth(22);
		c:SetHeight(22);
		c:SetPoint("TOPLEFT", panel, "TOPLEFT", lx, y + 5);
		local t = getglobal(c:GetName() .. "Text");
		if ( t ) then
			t:SetText("锁");
		end
		c:SetScript("OnClick", function()
			if ( this:GetChecked() ) then
				send(CMD.LOCK, i, value());
			else
				send(CMD.UNLOCK, i, 0);
			end
		end);
		row.lock = c;
	end

	eb:SetScript("OnEnterPressed", function()
		this:ClearFocus();
		if ( kind ~= "player" ) then        -- the game recomputes stats: Enter locks
			if ( row.lock ) then
				row.lock:SetChecked(1);
			end
			send(CMD.LOCK, i, value());
		else
			send(CMD.WRITE, i, value());
		end
	end);
	rows[i] = row;
end

-- 角色 on the left, 冒险属性 on the right, 状态 under both (four rows a side)
local LEFT, RIGHT = PAD, PAD + COL_W + GAP;
local groups, order = {}, {};
for i = 1, table.getn(FIELDS) do
	local g = FIELDS[i][7];
	if ( not groups[g] ) then
		groups[g] = {};
		table.insert(order, g);
	end
	table.insert(groups[g], i);
end
local function column(list, x, y, from, to)
	for k = from, to do
		addRow(list[k], x, y);
		y = y - ROW_H;
	end
	return y;
end
local yl, yr = TOP - 24, TOP - 24;
heading(order[1], LEFT, TOP, COL_W);
heading(order[2], RIGHT, TOP, COL_W);
yl = column(groups[order[1]], LEFT, yl, 1, table.getn(groups[order[1]]));
yr = column(groups[order[2]], RIGHT, yr, 1, table.getn(groups[order[2]]));
local y = math.min(yl, yr) - 12;
local st = groups[order[3]];
local half = math.ceil(table.getn(st) / 2);
heading(order[3], LEFT, y, COL_W * 2 + GAP);
yl = column(st, LEFT, y - 24, 1, half);
yr = column(st, RIGHT, y - 24, half + 1, table.getn(st));
y = math.min(yl, yr) - 14;

-- 极速模式: the run timer (what boss arrivals go by) at N x; combat keeps its speed.
-- The timer sits in the player object; "找计时中…" shows only if it has to be searched for.
local turboLabel = panel:CreateFontString(nil, "OVERLAY", "GameFontNormal");
turboLabel:SetPoint("TOPLEFT", panel, "TOPLEFT", LEFT, y - 4);
turboLabel:SetText("极速模式");
local TURBO_SPEEDS = { 1, 2, 4, 8 };
local turboButtons = {};
for k = 1, table.getn(TURBO_SPEEDS) do
	local speed = TURBO_SPEEDS[k];
	local b = CreateFrame("Button", "AZSRemoteTurbo" .. speed, panel, "UIPanelButtonTemplate");
	b:SetWidth(44);
	b:SetHeight(22);
	b:SetPoint("TOPLEFT", panel, "TOPLEFT", LEFT + 72 + (k - 1) * 48, y);
	b:SetScript("OnClick", function() send(CMD.TURBO, 0, speed); end);
	turboButtons[k] = b;
end
local turboStatus = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
turboStatus:SetPoint("TOPLEFT", panel, "TOPLEFT", LEFT + 72 + 4 * 48 + 8, y - 5);
turboStatus:SetText("");

local function button(name, text, right, onclick)
	local b = CreateFrame("Button", name, panel, "UIPanelButtonTemplate");
	b:SetWidth(88);
	b:SetHeight(22);
	b:SetPoint("TOPRIGHT", panel, "TOPLEFT", RIGHT + COL_W - right, y);
	b:SetText(text);
	b:SetScript("OnClick", onclick);
end
button("AZSRemoteRelocate", "重新定位", 0, function() send(CMD.RELOCATE); end);
button("AZSRemoteUnlock", "全部解锁", 94, function() send(CMD.UNLOCK_ALL); end);
button("AZSRemoteHeal", "回满血", 188, function() send(CMD.HEAL); end);
-- 锁满血, left of 回满血: health kept at 生命值 (raise that with a lock for more)
local full = CreateFrame("CheckButton", "AZSRemoteKeepFull", panel, "UICheckButtonTemplate");
full:SetWidth(22);
full:SetHeight(22);
full:SetPoint("TOPLEFT", panel, "TOPLEFT", RIGHT + COL_W - 188 - 88 - 72, y);
local fullText = getglobal(full:GetName() .. "Text");
if ( fullText ) then
	fullText:SetText("锁满血");
end
full:SetScript("OnClick", function()
	send(CMD.KEEP_FULL, 0, this:GetChecked() and 1 or 0);
end);
panel:SetHeight(-y + 22 + 22);           -- the button line, then the border

-- --- floating toggle button ----------------------------------------------------
-- Click: open/close the panel. Drag: move it. It shows the agent's state.
local function togglePanel()
	if ( panel:IsVisible() ) then
		panel:Hide();
	else
		panel:Show();
	end
end

-- It is the trainer's icon (icon.tga next to this file, drawn by src/make_icon.py)
-- with a dot in the state's colour. A client that can't load the picture gets the
-- text button instead: "AZS" in the state's colour.
local ICON_DIR = "Interface\\AddOns\\AZSRemote\\";
local ICON = ICON_DIR .. "icon";
local function iconLoads()
	local ok, probe = pcall(UIParent.CreateTexture, UIParent, nil, "BACKGROUND");
	if ( not ok or not probe ) then
		return false;
	end
	local set, loaded = pcall(probe.SetTexture, probe, ICON);
	local has, path = pcall(probe.GetTexture, probe);
	pcall(probe.Hide, probe);
	return set and (loaded or (has and path)) and true or false;
end

local toggle, toggleDot;
if ( iconLoads() ) then
	toggle = CreateFrame("Button", "AZSRemoteToggle", UIParent);
	toggle:SetWidth(28);
	toggle:SetHeight(28);
	local art = toggle:CreateTexture(nil, "ARTWORK");
	art:SetTexture(ICON);
	art:SetWidth(28);
	art:SetHeight(28);
	art:SetPoint("CENTER", toggle, "CENTER", 0, 0);
	-- the dot is a picture too (dot_red / dot_yellow / dot_green.tga): no font glyph needed
	toggleDot = toggle:CreateTexture(nil, "OVERLAY");
	toggleDot:SetWidth(10);
	toggleDot:SetHeight(10);
	toggleDot:SetPoint("BOTTOMRIGHT", toggle, "BOTTOMRIGHT", 2, -2);
else
	toggle = CreateFrame("Button", "AZSRemoteToggle", UIParent, "UIPanelButtonTemplate");
	toggle:SetWidth(52);
	toggle:SetHeight(22);
	toggle:SetText("AZS");
end
toggle:SetPoint("LEFT", UIParent, "LEFT", 16, 120);
toggle:SetFrameStrata("HIGH");
toggle:SetMovable(true);
toggle:RegisterForDrag("LeftButton");
local dragged = false;
toggle:SetScript("OnDragStart", function()
	dragged = true;
	this:StartMoving();
end);
toggle:SetScript("OnDragStop", function()
	this:StopMovingOrSizing();
	local point, _, relPoint, x, y = this:GetPoint();
	AZSRemoteDB = AZSRemoteDB or {};
	AZSRemoteDB.toggle = { point, relPoint, x, y };   -- kept if the game saves SavedVariables
end);
toggle:SetScript("OnClick", function()
	if ( dragged ) then                     -- the release that ends a drag is not a click
		dragged = false;
		return;
	end
	togglePanel();
end);

local DOT = { ["|cffff5555"] = "red", ["|cffffd100"] = "yellow", ["|cff55ff55"] = "green" };
local dotNow;
local function paintToggle(connected, st)
	local colour = "|cffff5555";            -- red: agent not running
	if ( connected ) then
		colour = (st == 2) and "|cff55ff55" or "|cffffd100";
	end
	if ( toggleDot ) then
		if ( DOT[colour] ~= dotNow ) then    -- set the picture only when the state changes
			dotNow = DOT[colour];
			toggleDot:SetTexture(ICON_DIR .. "dot_" .. dotNow);
		end
	else
		toggle:SetText(colour .. "AZS|r");
	end
end

-- --- talking to the agent ----------------------------------------------------
local TURBO_TEXT = { [1] = "找计时中…", [3] = "计时暂停", [4] = "没找到计时，稍后重试" };

-- run time the way the HUD writes it: 7:28
function AZSRemote_Clock(t)
	local m = math.floor(t / 60);
	return string.format("%d:%02d", m, math.floor(t - 60 * m));
end

local function paintTurbo(connected)
	local want = connected and MB[SLOT.TURBO_SPEED] or 0;
	for k = 1, table.getn(TURBO_SPEEDS) do
		local speed = TURBO_SPEEDS[k];
		local text = (speed == 1) and "关" or (speed .. "x");
		if ( speed == want ) then
			text = "|cff55ff55" .. text .. "|r";
		end
		turboButtons[k]:SetText(text);
	end
	local st, t = MB[SLOT.TURBO_STATE], MB[SLOT.TURBO_TIME];
	if ( not connected or st == 0 ) then
		turboStatus:SetText("");
	elseif ( st == 2 and t >= 0 ) then
		turboStatus:SetText("|cff55ff55局内 " .. AZSRemote_Clock(t) .. "|r");
	else
		turboStatus:SetText("|cffffd100" .. (TURBO_TEXT[st] or "") .. "|r");
	end
end

local RESULT_TEXT = { [0] = "完成", [1] = "后台还在定位，稍等", [2] = "输入有误", [3] = "执行失败（看 remote.bat 窗口）" };
local STATE_TEXT = { [1] = "定位中…（锁定会自动接上）", [2] = "已就绪", [3] = "角色位置变了，马上自动重新定位…" };

local poll = 0;
local driver = CreateFrame("Frame");
driver:SetScript("OnUpdate", function()
	poll = poll + (arg1 or 0);
	if ( poll < 0.1 ) then
		return;
	end
	poll = 0;
	-- "this array is alive": stale arrays of earlier runs stay in memory
	MB[SLOT.LUA_BEAT] = MB[SLOT.LUA_BEAT] + 1;
	if ( MB[SLOT.LUA_BEAT] > 1000000 ) then
		MB[SLOT.LUA_BEAT] = 1;
	end
	local now = GetTime();
	if ( MB[SLOT.HEARTBEAT] ~= lastBeat ) then
		lastBeat, lastBeatAt = MB[SLOT.HEARTBEAT], now;
	end
	local connected = now - lastBeatAt < 2 and lastBeat ~= 0;

	-- a reply to our last request?
	if ( waitingFor and MB[SLOT.ACK] == waitingFor ) then
		local r = MB[SLOT.RESULT];
		if ( r ~= 0 ) then
			say(RESULT_TEXT[r] or ("result " .. r));
		end
		waitingFor = nil;
	end
	-- post the next one, one at a time
	if ( not waitingFor and table.getn(queue) > 0 and MB[SLOT.ACK] == MB[SLOT.REQ] ) then
		local q = table.remove(queue, 1);
		MB[SLOT.CMD], MB[SLOT.ARG1], MB[SLOT.ARG2] = q[1], q[2], q[3];
		MB[SLOT.REQ] = MB[SLOT.REQ] + 1;     -- last: the agent acts when this changes
		waitingFor = MB[SLOT.REQ];
	end

	paintToggle(connected, MB[SLOT.STATE]);
	if ( not panel:IsVisible() ) then
		return;
	end
	if ( not connected ) then
		status:SetText("|cffff5555后台未连接：先双击仓库里的 remote.bat|r");
	else
		local st = MB[SLOT.STATE];
		local text = STATE_TEXT[st] or "";
		if ( table.getn(queue) > 0 or waitingFor ) then
			text = text .. "  ·  处理中…";
		end
		status:SetText((st == 2 and "|cff55ff55" or "|cffffd100") .. text .. "|r");
	end
	paintTurbo(connected);
	if ( connected and MB[SLOT.STATE] == 2 ) then
		local busy = waitingFor or table.getn(queue) > 0;   -- don't undo a fresh click
		for i = 1, table.getn(FIELDS) do
			local row = rows[i];
			row.cur:SetText(AZSRemote_Show(FIELDS[i][6], MB[SLOT.VALUES + i - 1]));
			paintValue(row, MB[SLOT.LOCKED + i - 1] == 1);
			if ( row.lock and not busy ) then
				row.lock:SetChecked(MB[SLOT.LOCKED + i - 1] == 1 and 1 or nil);
			end
		end
		if ( not busy ) then
			full:SetChecked(MB[SLOT.FULL_LOCKED] == 1 and 1 or nil);
		end
	end
end);

SLASH_AZSREMOTE1 = "/azs";
SlashCmdList["AZSREMOTE"] = togglePanel;

local hello = CreateFrame("Frame");
hello:RegisterEvent("PLAYER_ENTERING_WORLD");
hello:SetScript("OnEvent", function()
	local saved = AZSRemoteDB and AZSRemoteDB.toggle;
	if ( saved and saved[1] ) then
		toggle:ClearAllPoints();
		toggle:SetPoint(saved[1], UIParent, saved[2], saved[3], saved[4]);
	end
	say("控制器已加载：点屏幕左侧的 AZS 按钮打开（也可以 /azs）。需要先运行 remote.bat");
	this:UnregisterEvent("PLAYER_ENTERING_WORLD");
end);
