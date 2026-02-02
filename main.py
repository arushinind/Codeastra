import discord
from discord.ext import commands, tasks
import io
import contextlib
import textwrap
import traceback
import sys
import time
import ast
import os
import json
import asyncio
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

# --- CONFIGURATION ---
# Load environment variables from .env file (for local development)
load_dotenv()

# Get token from Environment Variable
TOKEN = os.getenv("DISCORD_TOKEN")

# Initialize Bot with necessary intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ✅ REMOVE DEFAULT HELP COMMAND
bot.remove_command('help')

# --- GLOBAL STATE ---
execution_history = []
user_stats = {}
MAX_HISTORY = 100
EXECUTION_TIMEOUT = 30
TRUSTED_USERS = set()

# --- CONFIGURATION FILE ---
# NOTE: On Railway (without a volume), this file will reset every time you redeploy.
CONFIG_FILE = "bot_config.json"

def load_config():
    """Load configuration from JSON file."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"trusted_users": [], "blocked_users": []}

def save_config(config):
    """Save configuration to JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

config = load_config()
TRUSTED_USERS = set(config.get("trusted_users", []))
BLOCKED_USERS = set(config.get("blocked_users", []))

# --- DECORATORS & UTILITIES ---
def is_trusted():
    """Check if user is trusted."""
    async def predicate(ctx):
        if ctx.author.id in TRUSTED_USERS or ctx.author.id == bot.owner_id:
            return True
        raise commands.CheckFailure("❌ You are not a trusted user. Ask the bot owner!")
    return commands.check(predicate)

def is_blocked():
    """Check if user is blocked."""
    async def predicate(ctx):
        if ctx.author.id in BLOCKED_USERS:
            raise commands.CheckFailure("🚫 You are blocked from using this bot.")
        return True
    return commands.check(predicate)

class CodeAnalyzer:
    """Analyzes code for safety and complexity."""
    
    DANGEROUS_KEYWORDS = {
        'os.system', 'subprocess', 'eval', '__import__',
        'open', 'exec', 'compile', 'globals', 'locals'
    }
    
    @staticmethod
    def analyze(code: str) -> tuple:
        """Returns (is_safe, message)"""
        try:
            tree = ast.parse(code)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        full_name = ast.unparse(node.func)
                        for dangerous in CodeAnalyzer.DANGEROUS_KEYWORDS:
                            if dangerous in full_name:
                                return False, f"⚠️ Dangerous operation detected: `{dangerous}`"
                    elif isinstance(node.func, ast.Name):
                        if node.func.id in CodeAnalyzer.DANGEROUS_KEYWORDS:
                            return False, f"⚠️ Dangerous operation detected: `{node.func.id}`"
            
            return True, "✅ Code analysis passed"
        except SyntaxError as e:
            return False, f"❌ Syntax Error: {e}"

# --- EVENTS ---
@bot.event
async def on_ready():
    """Bot startup event."""
    print(f"\n{'='*60}")
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"{'='*60}")
    print(f"🚀 Connected to {len(bot.guilds)} servers")
    print(f"👥 Serving {sum(g.member_count for g in bot.guilds)} members")
    print(f"{'='*60}\n")
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Python Code | !info"
        )
    )
    
    # Start background tasks
    if not update_status.is_running():
        update_status.start()

@bot.event
async def on_command_error(ctx, error):
    """Global error handler."""
    if isinstance(error, commands.CheckFailure):
        embed = discord.Embed(
            title="❌ Permission Denied",
            description=str(error),
            color=discord.Color.red()
        )
        await ctx.send(embed=embed, delete_after=10)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            title="❌ Missing Arguments",
            description=f"Usage: `{ctx.command.qualified_name} {ctx.command.signature}`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed, delete_after=10)
    else:
        print(f"Unhandled error: {error}")

# --- BACKGROUND TASKS ---
@tasks.loop(minutes=5)
async def update_status():
    """Update bot status with stats."""
    total_executions = sum(stats.get('executions', 0) for stats in user_stats.values())
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{total_executions} executions | !info"
        )
    )

# --- UTILITY FUNCTIONS ---
def clean_code(content: str) -> str:
    """Remove code blocks from string."""
    if content.startswith("```") and content.endswith("```"):
        return "\n".join(content.split("\n")[1:-1])
    return content.strip("` \n")

def format_code(code: str, language: str = "python") -> str:
    """Format code for display."""
    return f"```{language}\n{code}\n```"

def truncate(text: str, max_length: int = 1024) -> str:
    """Truncate text with ellipsis."""
    if len(text) > max_length:
        return text[:max_length-3] + "..."
    return text

def get_user_stats(user_id: int) -> dict:
    """Get or create user statistics."""
    if user_id not in user_stats:
        user_stats[user_id] = {
            'executions': 0,
            'errors': 0,
            'total_time': 0.0,
            'first_use': datetime.now().isoformat()
        }
    return user_stats[user_id]

# --- COMMANDS: EXECUTION ---
@bot.command(name="run", aliases=["exec", "eval", "py"])
@is_blocked()
async def run_code(ctx, *, code: str):
    """
    Execute Python code with full output capture.
    """
    
    # Prepare UI
    loading_embed = discord.Embed(
        description="⏳ Compiling and executing...",
        color=discord.Color.dark_theme()
    )
    status_msg = await ctx.send(embed=loading_embed)
    
    # Clean code
    code = clean_code(code)
    
    # Code analysis
    is_safe, analysis_msg = CodeAnalyzer.analyze(code)
    if not is_safe and ctx.author.id not in TRUSTED_USERS:
        embed = discord.Embed(
            title="🔒 Code Analysis Failed",
            description=analysis_msg,
            color=discord.Color.red()
        )
        await status_msg.edit(embed=embed)
        return
    
    # Setup execution environment
    local_variables = {
        "discord": discord,
        "commands": commands,
        "bot": bot,
        "ctx": ctx,
        "channel": ctx.channel,
        "author": ctx.author,
        "guild": ctx.guild,
        "message": ctx.message,
        "print": print,
    }
    
    stdout = io.StringIO()
    stderr = io.StringIO()
    
    start_time = time.time()
    
    try:
        # Wrap code in async function
        to_compile = f"async def func():\n{textwrap.indent(code, '  ')}"
        
        # Execute with output capture
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(to_compile, local_variables)
            await local_variables["func"]()
        
        execution_time = time.time() - start_time
        result = stdout.getvalue()
        
        # Update stats
        stats = get_user_stats(ctx.author.id)
        stats['executions'] += 1
        stats['total_time'] += execution_time
        
        # Add to history
        execution_history.append({
            'user': ctx.author.id,
            'code': code,
            'output': result,
            'timestamp': datetime.now().isoformat(),
            'status': 'success'
        })
        if len(execution_history) > MAX_HISTORY:
            execution_history.pop(0)
        
        # Build success embed
        embed = discord.Embed(
            title="🐍 Python Execution",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="📥 Input",
            value=format_code(truncate(code, 512)),
            inline=False
        )
        
        if result:
            embed.add_field(
                name="📤 Output",
                value=format_code(truncate(result, 512), "yaml"),
                inline=False
            )
        else:
            embed.add_field(
                name="📤 Output",
                value="```\n(No output - did you forget print?)\n```",
                inline=False
            )
        
        embed.add_field(
            name="⏱️ Execution Time",
            value=f"`{execution_time:.4f}s`",
            inline=True
        )
        
        embed.add_field(
            name="📊 Total Executions",
            value=f"`{stats['executions']}`",
            inline=True
        )
        
        embed.set_footer(
            text=f"Executed by {ctx.author.name}",
            icon_url=ctx.author.display_avatar.url
        )
        
        await status_msg.edit(embed=embed)
        
    except asyncio.TimeoutError:
        embed = discord.Embed(
            title="⏱️ Execution Timeout",
            description=f"Code took longer than {EXECUTION_TIMEOUT}s to execute.",
            color=discord.Color.red()
        )
        await status_msg.edit(embed=embed)
        
    except Exception as e:
        execution_time = time.time() - start_time
        
        # Update error stats
        stats = get_user_stats(ctx.author.id)
        stats['errors'] += 1
        
        # Add to history
        execution_history.append({
            'user': ctx.author.id,
            'code': code,
            'error': str(e),
            'timestamp': datetime.now().isoformat(),
            'status': 'error'
        })
        
        # Build error embed
        embed = discord.Embed(
            title="⚠️ Runtime Error",
            color=discord.Color.red(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="📥 Input",
            value=format_code(truncate(code, 512)),
            inline=False
        )
        
        full_traceback = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        embed.add_field(
            name="❌ Exception",
            value=format_code(truncate(full_traceback, 512), "py"),
            inline=False
        )
        
        embed.add_field(
            name="⏱️ Execution Time",
            value=f"`{execution_time:.4f}s`",
            inline=True
        )
        
        embed.set_footer(
            text=f"Executed by {ctx.author.name}",
            icon_url=ctx.author.display_avatar.url
        )
        
        await status_msg.edit(embed=embed)

# --- COMMANDS: MANAGEMENT ---
@bot.command(name="trust")
@commands.is_owner()
async def trust_user(ctx, user: discord.User):
    """Add a user to trusted list."""
    TRUSTED_USERS.add(user.id)
    config['trusted_users'] = list(TRUSTED_USERS)
    save_config(config)
    
    embed = discord.Embed(
        title="✅ User Trusted",
        description=f"{user.mention} can now execute dangerous code.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command(name="untrust")
@commands.is_owner()
async def untrust_user(ctx, user: discord.User):
    """Remove a user from trusted list."""
    TRUSTED_USERS.discard(user.id)
    config['trusted_users'] = list(TRUSTED_USERS)
    save_config(config)
    
    embed = discord.Embed(
        title="✅ User Untrusted",
        description=f"{user.mention} can no longer execute dangerous code.",
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed)

@bot.command(name="block")
@commands.is_owner()
async def block_user(ctx, user: discord.User):
    """Block a user from using the bot."""
    BLOCKED_USERS.add(user.id)
    config['blocked_users'] = list(BLOCKED_USERS)
    save_config(config)
    
    embed = discord.Embed(
        title="🚫 User Blocked",
        description=f"{user.mention} is now blocked.",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)

@bot.command(name="unblock")
@commands.is_owner()
async def unblock_user(ctx, user: discord.User):
    """Unblock a user."""
    BLOCKED_USERS.discard(user.id)
    config['blocked_users'] = list(BLOCKED_USERS)
    save_config(config)
    
    embed = discord.Embed(
        title="✅ User Unblocked",
        description=f"{user.mention} is now unblocked.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

# --- COMMANDS: STATISTICS ---
@bot.command(name="stats")
async def show_stats(ctx, user: Optional[discord.User] = None):
    """Show execution statistics."""
    target_user = user or ctx.author
    stats = get_user_stats(target_user.id)
    
    embed = discord.Embed(
        title=f"📊 Statistics for {target_user.name}",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="Total Executions",
        value=f"`{stats['executions']}`",
        inline=True
    )
    
    embed.add_field(
        name="Total Errors",
        value=f"`{stats['errors']}`",
        inline=True
    )
    
    embed.add_field(
        name="Success Rate",
        value=f"`{(stats['executions'] - stats['errors']) / max(stats['executions'], 1) * 100:.1f}%`",
        inline=True
    )
    
    embed.add_field(
        name="Total Execution Time",
        value=f"`{stats['total_time']:.2f}s`",
        inline=True
    )
    
    avg_time = stats['total_time'] / max(stats['executions'], 1)
    embed.add_field(
        name="Average Execution Time",
        value=f"`{avg_time:.4f}s`",
        inline=True
    )
    
    embed.add_field(
        name="First Use",
        value=f"`{stats['first_use']}`",
        inline=False
    )
    
    embed.set_thumbnail(url=target_user.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    """Show execution leaderboard."""
    if not user_stats:
        await ctx.send("No execution data yet!")
        return
    
    sorted_users = sorted(
        user_stats.items(),
        key=lambda x: x[1]['executions'],
        reverse=True
    )[:10]
    
    embed = discord.Embed(
        title="🏆 Execution Leaderboard",
        color=discord.Color.gold()
    )
    
    for i, (user_id, stats) in enumerate(sorted_users, 1):
        user = bot.get_user(user_id)
        user_name = user.name if user else f"Unknown ({user_id})"
        embed.add_field(
            name=f"#{i} {user_name}",
            value=f"Executions: `{stats['executions']}` | Errors: `{stats['errors']}`",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.command(name="history")
async def show_history(ctx, limit: int = 5):
    """Show recent execution history."""
    limit = min(limit, 10)
    recent = execution_history[-limit:][::-1]
    
    if not recent:
        await ctx.send("No execution history!")
        return
    
    embed = discord.Embed(
        title=f"📜 Recent Executions (Last {len(recent)})",
        color=discord.Color.blurple()
    )
    
    for entry in recent:
        user = bot.get_user(entry['user'])
        user_name = user.name if user else "Unknown"
        status = "✅" if entry['status'] == 'success' else "❌"
        
        code_preview = entry['code'][:50].replace('\n', ' ')
        if len(entry['code']) > 50:
            code_preview += "..."
        
        embed.add_field(
            name=f"{status} {user_name}",
            value=f"```\n{code_preview}\n```",
            inline=False
        )
    
    await ctx.send(embed=embed)

# --- COMMANDS: UTILITIES ---
@bot.command(name="analyze")
async def analyze_code(ctx, *, code: str):
    """Analyze code for safety and syntax."""
    code = clean_code(code)
    
    is_safe, message = CodeAnalyzer.analyze(code)
    
    embed = discord.Embed(
        title="🔍 Code Analysis",
        color=discord.Color.green() if is_safe else discord.Color.red()
    )
    
    embed.add_field(
        name="Code",
        value=format_code(truncate(code, 512)),
        inline=False
    )
    
    embed.add_field(
        name="Analysis Result",
        value=message,
        inline=False
    )
    
    try:
        tree = ast.parse(code)
        embed.add_field(
            name="📊 Complexity",
            value=f"Functions: `{len([n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)])}`\n"
                  f"Classes: `{len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)])}`\n"
                  f"Lines: `{len(code.split(chr(10)))}`",
            inline=False
        )
    except:
        pass
    
    await ctx.send(embed=embed)

@bot.command(name="sync")
@commands.is_owner()
async def sync_commands(ctx):
    """Sync slash commands."""
    embed = discord.Embed(
        title="⚙️ Synchronization",
        description="Syncing commands...",
        color=discord.Color.blue()
    )
    msg = await ctx.send(embed=embed)
    
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        
        embed.title = "✅ Synchronization Complete"
        embed.description = f"Successfully synced **{len(synced)}** commands."
        embed.color = discord.Color.green()
        await msg.edit(embed=embed)
    except Exception as e:
        embed.title = "❌ Synchronization Failed"
        embed.description = f"Error: {e}"
        embed.color = discord.Color.red()
        await msg.edit(embed=embed)

@bot.command(name="info")
async def help_command(ctx):
    """Show detailed help."""
    embed = discord.Embed(
        title="🤖 Python Executor Bot - Help",
        description="A powerful Discord bot for executing Python code safely.",
        color=discord.Color.blurple()
    )
    
    embed.add_field(
        name="🚀 Execution Commands",
        value="`!run <code>` - Execute Python code\n"
              "`!analyze <code>` - Analyze code for safety",
        inline=False
    )
    
    embed.add_field(
        name="📊 Statistics Commands",
        value="`!stats [user]` - Show execution stats\n"
              "`!leaderboard` - Show top executors\n"
              "`!history [limit]` - Show recent executions",
        inline=False
    )
    
    embed.add_field(
        name="🔐 Admin Commands",
        value="`!trust <user>` - Trust a user\n"
              "`!untrust <user>` - Untrust a user\n"
              "`!block <user>` - Block a user\n"
              "`!unblock <user>` - Unblock a user\n"
              "`!sync` - Sync slash commands",
        inline=False
    )
    
    embed.add_field(
        name="💡 Features",
        value="✅ Real-time code execution\n"
              "✅ Safety analysis\n"
              "✅ Execution statistics\n"
              "✅ User management\n"
              "✅ Execution history\n"
              "✅ Error tracking",
        inline=False
    )
    
    embed.set_footer(text="Use !info for more details")
    await ctx.send(embed=embed)

# --- MAIN ---
if __name__ == "__main__":
    if not TOKEN:
        print("\n" + "="*60)
        print("❌ ERROR: DISCORD_TOKEN is missing!")
        print("1. For Local: Create a .env file with DISCORD_TOKEN=your_token")
        print("2. For Railway: Add DISCORD_TOKEN in the Variables tab")
        print("="*60 + "\n")
        sys.exit(1)
    
    try:
        bot.run(TOKEN)
    except discord.errors.PrivilegedIntentsRequired:
        print("\n" + "="*60)
        print("❌ ERROR: Privileged Intents are missing!")
        print("1. Go to https://discord.com/developers/applications")
        print("2. Select your bot → Bot → Privileged Gateway Intents")
        print("3. Enable: Message Content Intent")
        print("="*60 + "\n")
    except discord.errors.LoginFailure:
        print("\n" + "="*60)
        print("❌ ERROR: Invalid token!")
        print("="*60 + "\n")
    except Exception as e:
        print(f"❌ Error: {e}")