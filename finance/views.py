import secrets
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import send_mail
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Sum, DecimalField, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST, require_http_methods

from .models import (Account, Role, AccountRole, UserProfile, Client,
                     Wallet, Transaction, Expense, ExpenseCategory,
                     Income, IncomeCategory, Goal)

CODE_LIFETIME = 600      # 10 минут
RESEND_DELAY = 60        # повторная отправка не чаще раза в минуту
MAX_ATTEMPTS = 5

# под DecimalField(max_digits=14, decimal_places=2)
MAX_AMOUNT = Decimal('999999999999.99')

WALLET_NAMES = {'cash': 'Наличные', 'card': 'Банковская карта'}

EXPENSE_CATEGORY_NAMES = {
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

INCOME_CATEGORY_NAMES = {
    'salary': 'Зарплата', 'bonus': 'Премия',
    'freelance': 'Подработка и фриланс', 'business': 'Доход от бизнеса',
    'investments': 'Инвестиции и проценты', 'rent': 'Сдача в аренду',
    'pension': 'Пенсия и пособия', 'gifts': 'Подарки',
    'sale': 'Продажа вещей', 'other': 'Другое',
}

CONFIRM_SESSION_KEYS = ('confirm_code', 'confirm_time', 'confirm_attempts',
                        'confirm_email', 'pending_registration')


# ---------- Вспомогательные функции ----------

def client_required(view):
    """Пускает только вошедших клиентов и передаёт client в представление."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        account_id = request.session.get('account_id')
        if not account_id:
            return redirect('enter_site')
        client = Client.objects.filter(account_id=account_id).first()
        if client is None:
            return redirect('enter_site')
        return view(request, client, *args, **kwargs)
    return wrapper


def get_or_create_first(model, **kwargs):
    """Как get_or_create, но не падает, если в БД уже есть дубликаты."""
    obj = model.objects.filter(**kwargs).order_by('pk').first()
    if obj is None:
        obj = model.objects.create(**kwargs)
    return obj


def parse_amount(raw):
    """Возвращает (Decimal, None) при успехе или (None, текст ошибки)."""
    raw = (raw or '').strip().replace(',', '.')
    if not raw:
        return None, 'Введите сумму'

    try:
        amount = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None, 'Введите корректную сумму'

    if not amount.is_finite():           # NaN, Infinity
        return None, 'Введите корректную сумму'

    try:
        amount = amount.quantize(Decimal('0.01'))
    except InvalidOperation:             # слишком большое число
        return None, 'Слишком большая сумма'

    if amount <= 0:
        return None, 'Сумма должна быть больше нуля'
    if amount > MAX_AMOUNT:
        return None, 'Слишком большая сумма'

    return amount, None


def parse_fio(fio):
    """'Иванов Иван Иванович' -> (фамилия, имя, отчество)"""
    parts = fio.split()
    last_name = parts[0] if len(parts) > 0 else ''
    first_name = parts[1] if len(parts) > 1 else ''
    middle_name = ' '.join(parts[2:]) if len(parts) > 2 else ''
    return last_name, first_name, middle_name


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

        # Пароль хранится только в виде хеша.
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
    birth_date = (date.fromisoformat(data['birth_date'])
                  if data['birth_date'] else None)

    with db_transaction.atomic():
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
            birth_date=birth_date,
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
        elif secrets.compare_digest(entered.encode(), saved.encode()):
            try:
                create_account_from_session(request)
            except IntegrityError:
                for key in CONFIRM_SESSION_KEYS:
                    request.session.pop(key, None)
                messages.error(request, 'Такой логин или email уже зарегистрирован.')
                return redirect('register_page')

            for key in CONFIRM_SESSION_KEYS:
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
    if not email or 'pending_registration' not in request.session:
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


# ---------- Вход, выход, главная ----------

def enter_site(request):
    if request.session.get('account_id'):
        return redirect('main_page')

    errors = {}
    login_value = ''

    if request.method == 'POST':
        login_value = request.POST.get('login', '').strip()
        password = request.POST.get('password', '')

        if not login_value:
            errors['login'] = 'Введите логин'
        elif len(login_value) > 150:
            errors['login'] = 'Логин слишком длинный'

        if not password:
            errors['password'] = 'Введите пароль'
        elif len(password) > 128:
            errors['password'] = 'Пароль слишком длинный'

        if not errors:
            account = Account.objects.filter(username=login_value).first()

            if account and check_password(password, account.password_hash):
                request.session.cycle_key()
                request.session['account_id'] = account.id
                return redirect('main_page')

            errors['common'] = 'Неверный логин или пароль'

    return render(request, 'enter_site.html', {
        'errors': errors,
        'login_value': login_value,
    })


@require_http_methods(['GET'])
@client_required
def main_page(request, client):
    return render(request, 'main_page.html')


@require_POST
def logout_view(request):
    request.session.flush()
    return redirect('enter_site')


# ---------- Расходы и доходы ----------

def add_transaction(request, client, *, template, url_name, category_names,
                    category_model, record_model, wallet_error,
                    category_error, success_text):
    """Общая логика добавления расхода или дохода."""
    form = {'wallet': '', 'category': '', 'amount': ''}

    if request.method == 'POST':
        form['wallet'] = request.POST.get('wallet', '')
        form['category'] = request.POST.get('category', '')
        form['amount'] = request.POST.get('amount', '').strip()

        amount, amount_error = parse_amount(form['amount'])

        if form['wallet'] not in WALLET_NAMES:
            messages.error(request, wallet_error)
        elif form['category'] not in category_names:
            messages.error(request, category_error)
        elif amount_error:
            messages.error(request, amount_error)
        else:
            with db_transaction.atomic():
                wallet = get_or_create_first(
                    Wallet, client=client, name=WALLET_NAMES[form['wallet']])
                category = get_or_create_first(
                    category_model, name=category_names[form['category']])
                tr = Transaction.objects.create(
                    amount=amount, date=date.today(), wallet=wallet)
                record_model.objects.create(transaction=tr, category=category)

            messages.success(request, success_text)
            return redirect(url_name)

    return render(request, template, {
        'wallets': list(WALLET_NAMES.items()),
        'categories': list(category_names.items()),
        'form': form,
    })


@require_http_methods(['GET', 'POST'])
@client_required
def expenses(request, client):
    return add_transaction(
        request, client,
        template='expenses.html',
        url_name='expenses',
        category_names=EXPENSE_CATEGORY_NAMES,
        category_model=ExpenseCategory,
        record_model=Expense,
        wallet_error='Выберите тип кошелька',
        category_error='Выберите категорию расходов',
        success_text='Расход сохранён',
    )


@require_http_methods(['GET', 'POST'])
@client_required
def income(request, client):
    return add_transaction(
        request, client,
        template='incomes.html',
        url_name='income',
        category_names=INCOME_CATEGORY_NAMES,
        category_model=IncomeCategory,
        record_model=Income,
        wallet_error='Выберите тип кошелька',
        category_error='Выберите категорию доходов',
        success_text='Доход сохранён',
    )


# ---------- Цели ----------

@require_http_methods(['GET', 'POST'])
@client_required
def goals(request, client):
    """GET — страница с формой и списком целей, POST — добавление цели."""
    errors = {}
    values = {'name': '', 'description': '', 'target_count': ''}

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        raw_amount = request.POST.get('target_count', '').strip()

        values = {'name': name, 'description': description,
                  'target_count': raw_amount}

        if not name:
            errors['name'] = 'Введите название цели'
        elif len(name) > 200:
            errors['name'] = 'Название не длиннее 200 символов'

        if len(description) > 2000:
            errors['description'] = 'Описание не длиннее 2000 символов'

        amount, amount_error = parse_amount(raw_amount)
        if amount_error:
            errors['target_count'] = amount_error

        if not errors:
            Goal.objects.create(
                client=client,
                name=name,
                description=description,
                target_count=amount,
            )
            messages.success(request, 'Цель добавлена')
            return redirect('goals')

    return render(request, 'goals.html', {
        'goals': client.goals.order_by('-id'),
        'errors': errors,
        'values': values,
    })


# ---------- Отчёт ----------

def _total(client, relation):
    """Сумма транзакций клиента, у которых есть запись в Income или Expense."""
    return Transaction.objects.filter(
        wallet__client=client, **{f'{relation}__isnull': False}
    ).aggregate(
        total=Coalesce(Sum('amount'), Value(Decimal('0.00')),
                       output_field=DecimalField(max_digits=16, decimal_places=2))
    )['total']


@require_http_methods(['GET'])
@client_required
def report(request, client):
    income_total = _total(client, 'incomes')
    expense_total = _total(client, 'expenses')

    recent = (Transaction.objects
              .filter(wallet__client=client)
              .select_related('wallet')
              .prefetch_related('incomes__category', 'expenses__category')
              .order_by('-date', '-id')[:10])

    operations = []
    for tr in recent:
        incomes = list(tr.incomes.all())
        expenses_ = list(tr.expenses.all())
        if incomes:
            kind, category = 'income', incomes[0].category.name
        elif expenses_:
            kind, category = 'expense', expenses_[0].category.name
        else:
            continue
        operations.append({
            'kind': kind,
            'category': category,
            'wallet': tr.wallet.name,
            'date': tr.date,
            'amount': tr.amount,
        })

    return render(request, 'report.html', {
        'balance': income_total - expense_total,
        'income_total': income_total,
        'expense_total': expense_total,
        'operations': operations,
    })