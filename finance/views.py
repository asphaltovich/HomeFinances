import secrets
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import send_mail
from django.db import transaction, IntegrityError
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from .models import (Account, Role, AccountRole, UserProfile, Client,
                     Wallet, Transaction, Expense, ExpenseCategory)

CODE_LIFETIME = 600      # 10 минут
RESEND_DELAY = 60        # повторная отправка не чаще раза в минуту
MAX_ATTEMPTS = 5

WALLET_NAMES = {'cash': 'Наличные', 'card': 'Банковская карта'}

CATEGORY_NAMES = {
    'food': 'Продукты', 'cafes': 'Кафе и рестораны', 'transport': 'Транспорт',
    'fuel': 'Топливо', 'housing': 'Жильё и аренда',
    'utilities': 'Коммунальные услуги', 'shopping': 'Одежда и обувь',
    'health': 'Здоровье и лекарства', 'beauty': 'Красота и уход',
    'entertainment': 'Развлечения', 'subscriptions': 'Подписки и сервисы',
    'communication': 'Связь и интернет', 'education': 'Образование',
    'travel': 'Путешествия', 'children': 'Дети',
    'pets': 'Домашние животные', 'household': 'Товары для дома',
    'gifts': 'Подарки и благотворительность', 'insurance': 'Страхование',
    'other': 'Другое',
}


# ---------- Регистрация и подтверждение ----------

def send_confirmation_code(request, email):
    code = f"{secrets.randbelow(10**6):06d}"

    request.session['confirm_code'] = code
    request.session['confirm_email'] = email
    request.session['confirm_time'] = time.time()
    request.session['confirm_attempts'] = 0

    send_mail(
        subject='Код подтверждения HomeFinances',
        message=(
            f'Ваш код подтверждения: {code}\n\n'
            'Код действует 10 минут.\n'
            'Если вы не регистрировались в HomeFinances, просто проигнорируйте это письмо.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


def parse_fio(fio):
    """'Иванов Иван Иванович' -> (фамилия, имя, отчество)"""
    parts = fio.split()
    last_name = parts[0] if len(parts) > 0 else ''
    first_name = parts[1] if len(parts) > 1 else ''
    middle_name = ' '.join(parts[2:]) if len(parts) > 2 else ''
    return last_name, first_name, middle_name


def registration_view(request):
    if request.method == 'POST':
        fio = request.POST.get('fio', '').strip()
        email = request.POST.get('email', '').strip().lower()
        dob = request.POST.get('dob', '').strip()
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        if not fio or not email or not username or not password:
            messages.error(request, 'Заполните ФИО, email, логин и пароль')
            return render(request, 'register_page.html')

        last_name, first_name, middle_name = parse_fio(fio)
        if not first_name:
            messages.error(request, 'Введите фамилию и имя (и отчество, если есть)')
            return render(request, 'register_page.html')

        birth_date = None
        if dob:
            try:
                birth_date = datetime.strptime(dob, '%d.%m.%Y').date()
            except ValueError:
                messages.error(request, 'Дата рождения должна быть в формате ДД.ММ.ГГГГ')
                return render(request, 'register_page.html')
            if birth_date > date.today():
                messages.error(request, 'Дата рождения не может быть в будущем')
                return render(request, 'register_page.html')

        if Account.objects.filter(email=email).exists():
            messages.error(request, 'Этот email уже зарегистрирован')
            return render(request, 'register_page.html')

        if Account.objects.filter(username=username).exists():
            messages.error(request, 'Этот логин уже занят')
            return render(request, 'register_page.html')

        # Данные ждут подтверждения; пароль хранится только в виде хеша.
        # Дата кладётся строкой: объект date не сериализуется в JSON.
        request.session['pending_registration'] = {
            'email': email,
            'username': username,
            'password_hash': make_password(password),
            'first_name': first_name,
            'last_name': last_name,
            'middle_name': middle_name,
            'birth_date': birth_date.isoformat() if birth_date else None,
        }

        try:
            send_confirmation_code(request, email)
        except Exception:
            messages.error(request, 'Не удалось отправить письмо. Попробуйте позже.')
            return render(request, 'register_page.html')

        return redirect('confirmation')

    return render(request, 'register_page.html')


def create_account_from_session(request):
    data = request.session['pending_registration']

    with transaction.atomic():
        account = Account.objects.create(
            username=data['username'],
            email=data['email'],
            password_hash=data['password_hash'],
        )
        role, _ = Role.objects.get_or_create(role_name='client')
        AccountRole.objects.create(account=account, role=role)
        UserProfile.objects.create(
            account=account,
            first_name=data['first_name'],
            last_name=data['last_name'],
            middle_name=data['middle_name'],
            birth_date=data['birth_date'],
        )
        Client.objects.create(account=account)

    return account


def confirmation(request):
    if 'confirm_code' not in request.session or 'pending_registration' not in request.session:
        return redirect('register_page')

    if request.method == 'POST':
        entered = request.POST.get('code', '').strip()
        saved = request.session['confirm_code']
        created = request.session.get('confirm_time', 0)
        attempts = request.session.get('confirm_attempts', 0)

        if attempts >= MAX_ATTEMPTS:
            messages.error(request, 'Слишком много попыток. Запросите новый код.')
        elif time.time() - created > CODE_LIFETIME:
            messages.error(request, 'Код устарел. Запросите новый.')
        elif secrets.compare_digest(entered, saved):
            try:
                create_account_from_session(request)
            except IntegrityError:
                request.session.flush()
                messages.error(request, 'Такой логин или email уже зарегистрирован.')
                return redirect('register_page')

            for key in ('confirm_code', 'confirm_time', 'confirm_attempts',
                        'confirm_email', 'pending_registration'):
                request.session.pop(key, None)
            messages.success(request, 'Email подтверждён. Войдите в аккаунт.')
            return redirect('enter_site')
        else:
            request.session['confirm_attempts'] = attempts + 1
            messages.error(request, 'Неверный код')

    return render(request, 'confirmation.html', {
        'email': request.session.get('confirm_email'),
    })


@require_POST
def resend_code(request):
    email = request.session.get('confirm_email')
    if not email:
        return redirect('register_page')

    if time.time() - request.session.get('confirm_time', 0) < RESEND_DELAY:
        messages.error(request, 'Подождите минуту перед повторной отправкой.')
        return redirect('confirmation')

    try:
        send_confirmation_code(request, email)
        messages.success(request, 'Новый код отправлен на почту.')
    except Exception:
        messages.error(request, 'Не удалось отправить письмо. Попробуйте позже.')

    return redirect('confirmation')


# ---------- Вход и остальные страницы ----------

def enter_site(request):
    # Если пользователь уже вошёл, сразу отправляем на главную
    if request.session.get('account_id'):
        return redirect('main_page')

    errors = {}
    login_value = ''

    if request.method == 'POST':
        login_value = request.POST.get('login', '').strip()
        password = request.POST.get('password', '')

        # Валидация
        if not login_value:
            errors['login'] = 'Введите логин'
        elif len(login_value) > 150:
            errors['login'] = 'Логин слишком длинный'

        if not password:
            errors['password'] = 'Введите пароль'
        elif len(password) > 128:
            errors['password'] = 'Пароль слишком длинный'

        # Проверка учётных данных
        if not errors:
            account = Account.objects.filter(username=login_value).first()

            if account and check_password(password, account.password_hash):
                request.session.cycle_key()  # новый ключ сессии при входе
                request.session['account_id'] = account.id
                return redirect('main_page')

            errors['common'] = 'Неверный логин или пароль'

    return render(request, 'enter_site.html', {
        'errors': errors,
        'login_value': login_value,
    })


def main_page(request):
    # Главная доступна только после входа
    if not request.session.get('account_id'):
        return redirect('enter_site')
    return render(request, 'main_page.html')


@require_POST
def logout_view(request):
    request.session.flush()   # полностью очищает сессию
    return redirect('enter_site')

def main_page(request):
    return render(request, 'main_page.html')


def expenses(request):
    account_id = request.session.get('account_id')
    if not account_id:
        return redirect('enter_site')

    try:
        client = Client.objects.get(account_id=account_id)
    except Client.DoesNotExist:
        return redirect('enter_site')

    if request.method == 'POST':
        wallet_key = request.POST.get('wallet')
        category_key = request.POST.get('category')

        try:
            amount = Decimal(request.POST.get('amount', ''))
        except InvalidOperation:
            amount = None

        if wallet_key not in WALLET_NAMES or category_key not in CATEGORY_NAMES:
            messages.error(request, 'Выберите кошелёк и категорию')
        elif amount is None or amount <= 0:
            messages.error(request, 'Введите корректную сумму')
        else:
            with transaction.atomic():
                wallet, _ = Wallet.objects.get_or_create(
                    client=client, name=WALLET_NAMES[wallet_key])
                category, _ = ExpenseCategory.objects.get_or_create(
                    name=CATEGORY_NAMES[category_key])
                tr = Transaction.objects.create(
                    amount=amount, date=date.today(), wallet=wallet)
                Expense.objects.create(transaction=tr, category=category)

            messages.success(request, 'Расход сохранён')
            return redirect('expenses')

    return render(request, 'expenses.html')