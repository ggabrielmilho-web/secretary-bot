import logging
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from bot.config import settings
from bot.database import crud
import bot.integrations.google_calendar as _gcal

logger = logging.getLogger(__name__)


def _menu_principal() -> InlineKeyboardMarkup:
    """Teclado de atalhos rápidos para usuários ativos."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Agenda de hoje", callback_data="menu_agenda"),
            InlineKeyboardButton("📊 Resumo do dia", callback_data="menu_resumo"),
        ],
        [
            InlineKeyboardButton("📋 Minhas tarefas", callback_data="menu_tarefas"),
            InlineKeyboardButton("⏰ Meus lembretes", callback_data="menu_lembretes"),
        ],
    ])


def _menu_novo_usuario() -> InlineKeyboardMarkup:
    """Teclado para usuário que acabou de se cadastrar."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 O que você pode fazer?", callback_data="menu_ajuda")],
        [
            InlineKeyboardButton(f"✅ Assinar — R$ {settings.PLAN_PRICE:.2f}/mês", callback_data="plan_monthly"),
            InlineKeyboardButton("🎟️ Tenho cupom", callback_data="plan_coupon"),
        ],
    ])


def _menu_trial_vencendo(dias: int) -> InlineKeyboardMarkup:
    """Menu para usuário em trial com poucos dias restantes."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Agenda de hoje", callback_data="menu_agenda"),
            InlineKeyboardButton("📊 Resumo do dia", callback_data="menu_resumo"),
        ],
        [
            InlineKeyboardButton("📋 Minhas tarefas", callback_data="menu_tarefas"),
            InlineKeyboardButton("⏰ Meus lembretes", callback_data="menu_lembretes"),
        ],
        [InlineKeyboardButton(
            f"⚡ Assinar agora — R$ {settings.PLAN_PRICE:.2f}/mês ({dias}d restantes)",
            callback_data="plan_monthly",
        )],
    ])


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra novo usuário com trial ou exibe menu rápido para usuário existente."""
    user = update.effective_user
    if not user:
        return

    db_user = await crud.get_user_by_telegram_id(user.id)

    if not db_user:
        # Novo usuário
        db_user = await crud.create_user(
            telegram_id=user.id,
            name=user.full_name or str(user.id),
            trial_days=settings.TRIAL_DURATION_DAYS,
        )
        await update.message.reply_text(
            f"👋 Olá, {user.first_name}! Sou seu secretário executivo pessoal.\n\n"
            "Posso te ajudar com:\n"
            "📋 Tarefas e atividades\n"
            "📅 Reuniões e compromissos\n"
            "⏰ Lembretes automáticos\n"
            "🎤 Comandos por áudio\n\n"
            f"Você tem *{settings.TRIAL_DURATION_DAYS} dias grátis* para testar!\n"
            "Pode falar comigo normalmente ou usar os atalhos abaixo 👇",
            parse_mode="Markdown",
            reply_markup=_menu_novo_usuario(),
        )
        logger.info(f"Novo usuário registrado: {user.id} ({user.full_name})")
        return

    active, _ = crud.check_subscription_status(db_user)

    if not active:
        await update.message.reply_text(
            f"Olá, {user.first_name}! Seu acesso expirou.\n\n"
            "Assine para continuar usando seu secretário pessoal:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ Assinar — R$ {settings.PLAN_PRICE:.2f}/mês",
                    callback_data="plan_monthly",
                )],
                [InlineKeyboardButton("🎟️ Tenho cupom", callback_data="plan_coupon")],
            ]),
        )
        return

    # Usuário ativo — verifica se trial está vencendo
    if db_user.plan == "trial" and db_user.trial_ends_at:
        dias_restantes = max((db_user.trial_ends_at - datetime.now()).days, 0)
        if dias_restantes <= 3:
            await update.message.reply_text(
                f"Olá, {user.first_name}! 👋\n"
                f"⚠️ Seu teste termina em *{dias_restantes} dia(s)*.\n\n"
                "O que deseja fazer?",
                parse_mode="Markdown",
                reply_markup=_menu_trial_vencendo(dias_restantes),
            )
            return
        await update.message.reply_text(
            f"Olá, {user.first_name}! 👋\n"
            f"_Trial: {dias_restantes} dia(s) restante(s)_\n\n"
            "O que deseja fazer?",
            parse_mode="Markdown",
            reply_markup=_menu_principal(),
        )
        return

    # Assinante ativo
    await update.message.reply_text(
        f"Olá, {user.first_name}! 😊\n\nO que deseja fazer?",
        reply_markup=_menu_principal(),
    )


async def handle_planos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe planos e opção de pagamento via PIX."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Assinar — R$ {settings.PLAN_PRICE:.2f}/mês via PIX",
            callback_data="plan_monthly",
        )],
        [InlineKeyboardButton("🎟️ Tenho um cupom", callback_data="plan_coupon")],
    ])

    await update.message.reply_text(
        f"🤖 *Secretário IA Pessoal* — R$ {settings.PLAN_PRICE:.2f}/mês\n\n"
        "✅ Tarefas, reuniões e lembretes ilimitados\n"
        "✅ Comandos por áudio (voz)\n"
        "✅ Resumo diário automático às 7h\n"
        "✅ Detecção de conflitos de agenda\n\n"
        "Pague via PIX e tenha acesso imediato:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def handle_renovar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias para /planos."""
    await handle_planos(update, context)


async def handle_cupom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ativa um cupom de desconto ou acesso gratuito."""
    user = update.effective_user
    if not user:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Digite o código do cupom assim: /cupom SEUCÓDIGO")
        return

    code = args[0].upper().strip()

    db_user = await crud.get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Use /start para criar sua conta primeiro.")
        return

    coupon = await crud.get_coupon(code)

    if not coupon or not coupon.is_active:
        await update.message.reply_text("❌ Cupom inválido ou inativo.")
        return

    if coupon.expires_at and coupon.expires_at < datetime.now():
        await update.message.reply_text("❌ Este cupom está expirado.")
        return

    if coupon.times_used >= coupon.max_uses:
        await update.message.reply_text("❌ Este cupom já foi utilizado o número máximo de vezes.")
        return

    if coupon.duration_days:
        from datetime import timedelta
        expires = datetime.now() + timedelta(days=coupon.duration_days)
    else:
        expires = None

    await crud.activate_plan(user_id=db_user.id, plan=coupon.plan, subscription_ends_at=expires)
    await crud.use_coupon(coupon.id)

    plan_label = "vitalício" if coupon.plan == "lifetime" else coupon.plan
    expiry_str = expires.strftime("%d/%m/%Y") if expires else "vitalício"

    await update.message.reply_text(
        f"✅ Cupom *{code}* ativado!\n\n"
        f"Plano: *{plan_label}* (válido até {expiry_str})\n\n"
        "Aproveite! Use os atalhos abaixo para começar 👇",
        parse_mode="Markdown",
        reply_markup=_menu_principal(),
    )
    logger.info(f"Cupom {code} ativado por usuário {user.id} — plano {coupon.plan}")


async def handle_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe instruções de uso com atalhos rápidos."""
    await update.message.reply_text(
        "🤖 *Secretário IA — Como usar*\n\n"
        "Fale comigo normalmente por texto ou áudio!\n\n"
        "*Exemplos:*\n"
        "• _'Agenda uma reunião com João amanhã às 14h'_\n"
        "• _'Cria uma tarefa: enviar proposta até sexta'_\n"
        "• _'Me lembra de ligar pro cliente às 9h'_\n"
        "• _'O que tenho hoje?'_\n\n"
        "*Comandos:*\n"
        "/planos — Ver planos e assinar\n"
        "/cupom CÓDIGO — Ativar cupom\n"
        "/ajuda — Esta mensagem\n\n"
        "Ou use os atalhos rápidos:",
        parse_mode="Markdown",
        reply_markup=_menu_principal(),
    )
