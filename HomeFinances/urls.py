from django.contrib import admin
from django.urls import path

from finance import views

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Добавляем путь для страницы регистрации
    path('register_page/', views.registration_view, name='register_page'),
    path('enter_site/', views.enter_site, name='enter_site'),
]