-- AZS Remote: in-game panel for the azeroth-survivors-trainer agent.
--
-- The Lua sandbox cannot reach any outside program (io/os/require are nil and
-- LaunchURL is gone inside a run), so the agent (remote.bat -> src/agent.py)
-- comes to us: it finds AZSRemote_MB in memory by its magic numbers and reads
-- and writes its slots. This file only fills in requests and shows replies.
--
-- Slot numbers and the field list must match src/addon_mailbox.py and
-- src/session.py FIELDS — tests/test_offline.py checks both.

local PROTOCOL = 5;      -- bump with any slot or field-order change, as in src/addon_mailbox.py
local SLOT = { REQ = 4, CMD = 5, ARG1 = 6, ARG2 = 7, ACK = 8, RESULT = 9,
	HEARTBEAT = 10, STATE = 11, LUA_BEAT = 12, VALUES = 13, LOCKED = 43, SIZE = 72 };
local CMD = { WRITE = 1, LOCK = 2, UNLOCK = 3, HEAL = 4, UNLOCK_ALL = 5, RELOCATE = 6 };

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
	{ "effect", "lifesteal", "生命偷取（装备部分）", "f", true, "%.1", "冒险属性" },
	{ "stat", "heal", "击杀治疗", "f", true, "num", "冒险属性" },
	{ "effect", "thorns", "荆棘", "f", true, "%", "冒险属性" },
	{ "stat", "lives", "生命数", "i", true, "int", "冒险属性" },
	{ "stat", "xp", "经验值", "f", true, "+%1", "冒险属性" },
	{ "stat", "gold", "金币", "f", true, "+%1", "冒险属性" },
	{ "stat", "luck", "幸运", "f", true, "+%", "冒险属性" },
	{ "stat", "bp", "增益强度", "f", true, "+%", "冒险属性" },
	{ "stat", "dots", "持续伤害层数", "i", true, "dots", "冒险属性" },
	{ "stat", "doth", "持续伤害急速", "f", true, "+%", "冒险属性" },
	{ "player", "gold", "金币数量", "i", true, "int", "状态" },
	{ "player", "hp", "当前血量", "f", true, "num", "状态" },
	{ "player", "level", "等级", "i", false, "int", "状态" },
	{ "player", "xp", "当前经验", "i", false, "int", "状态" },
	{ "player", "kills", "击杀", "i", false, "int", "状态" },
	{ "player", "rerolls", "重抽次数", "i", true, "int", "状态" },
	{ "player", "banishes", "放逐次数", "i", true, "int", "状态" },
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
local ROW_H, TOP = 20, -58;
local panel = CreateFrame("Frame", "AZSRemoteFrame", UIParent);
panel:SetWidth(430);
panel:SetHeight(-TOP + ROW_H * (table.getn(FIELDS) + 2) + 64);
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

local title = panel:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge");
title:SetPoint("TOP", panel, "TOP", 0, -18);
title:SetText("AZS 控制器");

local status = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
status:SetPoint("TOPLEFT", panel, "TOPLEFT", 20, -40);
status:SetText("");

local close = CreateFrame("Button", "AZSRemoteClose", panel, "UIPanelCloseButton");
close:SetPoint("TOPRIGHT", panel, "TOPRIGHT", -6, -6);

local function addRow(i, y)
	local kind, label, lockable, unit = FIELDS[i][1], FIELDS[i][3], FIELDS[i][5], FIELDS[i][6];
	local row = {};
	local l = panel:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall");
	l:SetPoint("TOPLEFT", panel, "TOPLEFT", 22, y);
	l:SetText(label);
	row.cur = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
	row.cur:SetPoint("TOPRIGHT", panel, "TOPLEFT", 250, y);
	row.cur:SetJustifyH("RIGHT");
	row.cur:SetText("-");

	local eb = CreateFrame("EditBox", "AZSRemoteEdit" .. i, panel, "InputBoxTemplate");
	eb:SetWidth(64);
	eb:SetHeight(18);
	eb:SetPoint("TOPLEFT", panel, "TOPLEFT", 264, y + 3);
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

	if ( kind == "player" ) then
		local b = CreateFrame("Button", "AZSRemoteWrite" .. i, panel, "UIPanelButtonTemplate");
		b:SetWidth(36);
		b:SetHeight(18);
		b:SetPoint("TOPLEFT", panel, "TOPLEFT", 332, y + 3);
		b:SetText("写");
		b:SetScript("OnClick", function() send(CMD.WRITE, i, value()); end);
	end
	if ( lockable ) then
		local c = CreateFrame("CheckButton", "AZSRemoteLock" .. i, panel, "UICheckButtonTemplate");
		c:SetWidth(22);
		c:SetHeight(22);
		c:SetPoint("TOPLEFT", panel, "TOPLEFT", 372, y + 5);
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

-- rows under headings, as the game groups them: 角色 / 冒险属性 / 状态
local y = TOP;
for i = 1, table.getn(FIELDS) do
	if ( i == 1 or FIELDS[i][7] ~= FIELDS[i - 1][7] ) then
		if ( i > 1 ) then
			y = y - 6;
		end
		local h = panel:CreateFontString(nil, "OVERLAY", "GameFontNormal");
		h:SetPoint("TOPLEFT", panel, "TOPLEFT", 18, y);
		h:SetText("|cffffd100" .. FIELDS[i][7] .. "|r");
		y = y - 18;
	end
	addRow(i, y);
	y = y - ROW_H;
end
panel:SetHeight(-y + 60);                  -- room for the buttons at the bottom

local function button(name, text, x, onclick)
	local b = CreateFrame("Button", name, panel, "UIPanelButtonTemplate");
	b:SetWidth(96);
	b:SetHeight(22);
	b:SetPoint("BOTTOMLEFT", panel, "BOTTOMLEFT", x, 18);
	b:SetText(text);
	b:SetScript("OnClick", onclick);
end
button("AZSRemoteHeal", "回满血", 22, function() send(CMD.HEAL); end);
button("AZSRemoteUnlock", "全部解锁", 124, function() send(CMD.UNLOCK_ALL); end);
button("AZSRemoteRelocate", "重新定位", 226, function() send(CMD.RELOCATE); end);

-- --- floating toggle button ----------------------------------------------------
-- Click: open/close the panel. Drag: move it. Its colour is the agent's state.
local function togglePanel()
	if ( panel:IsVisible() ) then
		panel:Hide();
	else
		panel:Show();
	end
end

local toggle = CreateFrame("Button", "AZSRemoteToggle", UIParent, "UIPanelButtonTemplate");
toggle:SetWidth(52);
toggle:SetHeight(22);
toggle:SetPoint("LEFT", UIParent, "LEFT", 16, 120);
toggle:SetFrameStrata("HIGH");
toggle:SetText("AZS");
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

local function paintToggle(connected, st)
	local colour = "|cffff5555";            -- red: agent not running
	if ( connected ) then
		colour = (st == 2) and "|cff55ff55" or "|cffffd100";
	end
	toggle:SetText(colour .. "AZS|r");
end

-- --- talking to the agent ----------------------------------------------------
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
	if ( connected and MB[SLOT.STATE] == 2 ) then
		local busy = waitingFor or table.getn(queue) > 0;   -- don't undo a fresh click
		for i = 1, table.getn(FIELDS) do
			local row = rows[i];
			row.cur:SetText(AZSRemote_Show(FIELDS[i][6], MB[SLOT.VALUES + i - 1]));
			if ( row.lock and not busy ) then
				row.lock:SetChecked(MB[SLOT.LOCKED + i - 1] == 1 and 1 or nil);
			end
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
