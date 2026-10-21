from django.shortcuts import render


def registration_view(request):
    return render(request, 'register_page.html')
def enter_site(request):
    return render(request, 'enter_site.html')